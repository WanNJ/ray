import time
import asyncio
from functools import wraps
from dataclasses import dataclass
from inspect import iscoroutinefunction, isasyncgenfunction
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    overload,
    Tuple,
    TypeVar,
    AsyncGenerator,
    Iterable,
)

from ray.util.annotations import PublicAPI
from ray.serve.exceptions import RayServeException
from ray._private.utils import get_or_create_event_loop
from ray.serve._private.utils import extract_self_if_method_call
from ray._private.signature import extract_signature, flatten_args, recover_args


@dataclass
class _SingleRequest:
    self_arg: Any
    flattened_args: List[Any]
    future: asyncio.Future


@dataclass
class _GeneratorResult:
    result: Any
    next_future: asyncio.Future


def _batch_args_kwargs(
    list_of_flattened_args: List[List[Any]],
) -> Tuple[Tuple[Any], Dict[Any, Any]]:
    """Batch a list of flatten args and returns regular args and kwargs"""
    # Ray's flatten arg format is a list with alternating key and values
    # e.g. args=(1, 2), kwargs={"key": "val"} got turned into
    #      [None, 1, None, 2, "key", "val"]
    arg_lengths = {len(args) for args in list_of_flattened_args}
    assert (
        len(arg_lengths) == 1
    ), "All batch requests should have the same number of parameters."
    arg_length = arg_lengths.pop()

    batched_flattened_args = []
    for idx in range(arg_length):
        if idx % 2 == 0:
            batched_flattened_args.append(list_of_flattened_args[0][idx])
        else:
            batched_flattened_args.append(
                [item[idx] for item in list_of_flattened_args]
            )

    return recover_args(batched_flattened_args)


class _BatchQueue:
    def __init__(
        self,
        max_batch_size: int,
        timeout_s: float,
        handle_batch_func: Optional[Callable] = None,
    ) -> None:
        """Async queue that accepts individual items and returns batches.

        Respects max_batch_size and timeout_s; a batch will be returned when
        max_batch_size elements are available or the timeout has passed since
        the previous get.

        If handle_batch_func is passed in, a background coroutine will run to
        poll from the queue and call handle_batch_func on the results.

        Arguments:
            max_batch_size: max number of elements to return in a batch.
            timeout_s: time to wait before returning an incomplete
                batch.
            handle_batch_func(Optional[Callable]): callback to run in the
                background to handle batches if provided.
        """
        self.queue: asyncio.Queue[_SingleRequest] = asyncio.Queue()
        self.full_batch_event = asyncio.Event()
        self.max_batch_size = max_batch_size
        self.timeout_s = timeout_s

        self._handle_batch_task = None
        if handle_batch_func is not None:
            self._handle_batch_task = get_or_create_event_loop().create_task(
                self._process_batches(handle_batch_func)
            )

    def put(self, request: Tuple[_SingleRequest, asyncio.Future]) -> None:
        self.queue.put_nowait(request)
        # Signal when the full batch is ready. The event will be reset
        # in wait_for_batch.
        if self.queue.qsize() == self.max_batch_size:
            self.full_batch_event.set()

    async def wait_for_batch(self) -> List[Any]:
        """Wait for batch respecting self.max_batch_size and self.timeout_s.

        Returns a batch of up to self.max_batch_size items, waiting for up
        to self.timeout_s for a full batch. After the timeout, returns as many
        items as are ready.

        Always returns a batch with at least one item - will block
        indefinitely until an item comes in.
        """
        curr_timeout = self.timeout_s
        batch = []
        while len(batch) == 0:
            loop_start = time.time()

            # If the timeout is 0, wait for any item to be available on the
            # queue.
            if curr_timeout == 0:
                batch.append(await self.queue.get())
            # If the timeout is nonzero, wait for either the timeout to occur
            # or the max batch size to be ready.
            else:
                try:
                    await asyncio.wait_for(self.full_batch_event.wait(), curr_timeout)
                except asyncio.TimeoutError:
                    pass

            # Pull up to the max_batch_size requests off the queue.
            while len(batch) < self.max_batch_size and not self.queue.empty():
                batch.append(self.queue.get_nowait())

            # Reset the event if there are fewer than max_batch_size requests
            # in the queue.
            if (
                self.queue.qsize() < self.max_batch_size
                and self.full_batch_event.is_set()
            ):
                self.full_batch_event.clear()

            # Adjust the timeout based on the time spent in this iteration.
            curr_timeout = max(0, curr_timeout - (time.time() - loop_start))

        return batch

    def _validate_results(
        self, results: Iterable[Any], input_batch_length: int
    ) -> None:
        if len(results) != input_batch_length:
            raise RayServeException(
                "Batched function doesn't preserve batch size. "
                f"The input list has length {input_batch_length} but the "
                f"returned list has length {len(results)}."
            )

    async def _consume_func_generator(
        self,
        func_generator: AsyncGenerator,
        initial_futures: List[asyncio.Future],
        input_batch_length: int,
    ) -> None:
        """Consumes batch function generator.

        This function only runs if the function decorated with @serve.batch
        is a generator.
        """

        try:
            futures = initial_futures
            async for results in func_generator:
                self._validate_results(results, input_batch_length)
                next_futures = [
                    get_or_create_event_loop().create_future() for _ in futures
                ]
                for result, (future, next_future) in zip(
                    results, zip(futures, next_futures)
                ):
                    future.set_result(_GeneratorResult(result, next_future))
                futures = next_futures

            for future in futures:
                future.set_exception(StopAsyncIteration)
        except Exception as e:
            for future in futures:
                future.set_exception(e)

    async def _process_batches(self, func: Callable) -> None:
        """Loops infinitely and processes queued request batches."""

        while True:
            batch: List[_SingleRequest] = await self.wait_for_batch()
            assert len(batch) > 0
            self_arg = batch[0].self_arg
            args, kwargs = _batch_args_kwargs([item.flattened_args for item in batch])
            futures = [item.future for item in batch]

            # Method call.
            if self_arg is not None:
                func_future_or_generator = func(self_arg, *args, **kwargs)
            # Normal function call.
            else:
                func_future_or_generator = func(*args, **kwargs)

            if isasyncgenfunction(func):
                func_generator = func_future_or_generator
                await self._consume_func_generator(func_generator, futures, len(batch))
            else:
                try:
                    func_future = func_future_or_generator
                    results = await func_future
                    self._validate_results(results, len(batch))
                    for result, future in zip(results, futures):
                        future.set_result(result)
                except Exception as e:
                    for future in futures:
                        future.set_exception(e)

    def __del__(self):
        if (
            self._handle_batch_task is None
            or not get_or_create_event_loop().is_running()
        ):
            return

        # TODO(edoakes): although we try to gracefully shutdown here, it still
        # causes some errors when the process exits due to the asyncio loop
        # already being destroyed.
        self._handle_batch_task.cancel()


T = TypeVar("T")
R = TypeVar("R")
F = TypeVar("F", bound=Callable[[List[T]], List[R]])
G = TypeVar("G", bound=Callable[[T], R])


# Normal decorator use case (called with no arguments).
@overload
def batch(func: F) -> G:
    pass


# "Decorator factory" use case (called with arguments).
@overload
def batch(
    max_batch_size: int = 10, batch_wait_timeout_s: float = 0.0
) -> Callable[[F], G]:
    pass


@PublicAPI(stability="beta")
def batch(
    _func: Optional[Callable] = None,
    max_batch_size: int = 10,
    batch_wait_timeout_s: float = 0.0,
):
    """Converts a function to asynchronously handle batches.

    The function can be a standalone function or a class method. In both
    cases, the function must be `async def` and take a list of objects as
    its sole argument and return a list of the same length as a result.

    When invoked, the caller passes a single object. These will be batched
    and executed asynchronously once there is a batch of `max_batch_size`
    or `batch_wait_timeout_s` has elapsed, whichever occurs first.

    Example:

    .. code-block:: python

            from ray import serve
            from starlette.requests import Request

            @serve.deployment
            class BatchedDeployment:
                @serve.batch(max_batch_size=10, batch_wait_timeout_s=0.1)
                async def batch_handler(self, requests: List[Request]) -> List[str]:
                    response_batch = []
                    for r in requests:
                        name = (await requests.json())["name"]
                        response_batch.append(f"Hello {name}!")

                    return response_batch

                async def __call__(self, request: Request):
                    return await self.batch_handler(request)

            app = BatchedDeployment.bind()

    Arguments:
        max_batch_size: the maximum batch size that will be executed in
            one call to the underlying function.
        batch_wait_timeout_s: the maximum duration to wait for
            `max_batch_size` elements before running the current batch.
    """
    # `_func` will be None in the case when the decorator is parametrized.
    # See the comment at the end of this function for a detailed explanation.
    if _func is not None:
        if not callable(_func):
            raise TypeError(
                "@serve.batch can only be used to decorate functions or methods."
            )

        if not iscoroutinefunction(_func):
            raise TypeError("Functions decorated with @serve.batch must be 'async def'")

    if not isinstance(max_batch_size, int):
        if isinstance(max_batch_size, float) and max_batch_size.is_integer():
            max_batch_size = int(max_batch_size)
        else:
            raise TypeError("max_batch_size must be integer >= 1")

    if max_batch_size < 1:
        raise ValueError("max_batch_size must be an integer >= 1")

    if not isinstance(batch_wait_timeout_s, (float, int)):
        raise TypeError("batch_wait_timeout_s must be a float >= 0")

    if batch_wait_timeout_s < 0:
        raise ValueError("batch_wait_timeout_s must be a float >= 0")

    def _batch_decorator(_func):
        async def batch_handler_generator(
            first_future: asyncio.Future,
        ) -> AsyncGenerator:
            """Generator that handles generator batch functions."""

            future = first_future
            while True:
                try:
                    async_response: _GeneratorResult = await future
                    future = async_response.next_future
                    yield async_response.result
                except StopAsyncIteration:
                    break

        def enqueue_request(args, kwargs) -> asyncio.Future:
            self = extract_self_if_method_call(args, _func)
            flattened_args: List = flatten_args(extract_signature(_func), args, kwargs)

            if self is None:
                # For functions, inject the batch queue as an
                # attribute of the function.
                batch_queue_object = _func
            else:
                # For methods, inject the batch queue as an
                # attribute of the object.
                batch_queue_object = self
                # Trim the self argument from methods
                flattened_args = flattened_args[2:]

            # The first time the function runs, we lazily construct the batch
            # queue and inject it under a custom attribute name. On subsequent
            # runs, we just get a reference to the attribute.
            batch_queue_attr = f"__serve_batch_queue_{_func.__name__}"
            if not hasattr(batch_queue_object, batch_queue_attr):
                batch_queue = _BatchQueue(max_batch_size, batch_wait_timeout_s, _func)
                setattr(batch_queue_object, batch_queue_attr, batch_queue)
            else:
                batch_queue = getattr(batch_queue_object, batch_queue_attr)

            future = get_or_create_event_loop().create_future()
            batch_queue.put(_SingleRequest(self, flattened_args, future))
            return future

        @wraps(_func)
        def generator_batch_wrapper(*args, **kwargs):
            first_future = enqueue_request(args, kwargs)
            return batch_handler_generator(first_future)

        @wraps(_func)
        async def batch_wrapper(*args, **kwargs):
            # This will raise if the underlying call raised an exception.
            return await enqueue_request(args, kwargs)

        if isasyncgenfunction(_func):
            return generator_batch_wrapper
        else:
            return batch_wrapper

    # Unfortunately, this is required to handle both non-parametrized
    # (@serve.batch) and parametrized (@serve.batch(**kwargs)) usage.
    # In the former case, `serve.batch` will be called with the underlying
    # function as the sole argument. In the latter case, it will first be
    # called with **kwargs, then the result of that call will be called
    # with the underlying function as the sole argument (i.e., it must be a
    # "decorator factory.").
    return _batch_decorator(_func) if callable(_func) else _batch_decorator
