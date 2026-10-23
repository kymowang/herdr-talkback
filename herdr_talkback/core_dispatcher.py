from __future__ import annotations

import asyncio
import json
import time
import uuid
from functools import partial
from typing import Callable, Dict, List, Mapping, Set, Tuple

from .core_types import (
    AdapterDeliveryState,
    CoreMessage,
    CoreMessageState,
    DeliveryLease,
)
from .endpoints import EndpointNotFound


RECOVERABLE_ADAPTERS = ('peer',)
DEFAULT_RETRY_DELAY_SECONDS = 1.0


class CoreDispatcher:
    def __init__(
        self,
        state,
        registry,
        adapters: Mapping[str, object],
        lease_id_factory: Callable[[], str] = None,
        *,
        retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS,
    ) -> None:
        if retry_delay_seconds <= 0:
            raise ValueError("retry delay must be positive")
        self.state = state
        self.registry = registry
        self.adapters = adapters
        self.lease_id_factory = lease_id_factory or (lambda: uuid.uuid4().hex)
        self.retry_delay_seconds = float(retry_delay_seconds)
        self._endpoint_tasks: Dict[str, asyncio.Task] = {}
        self._pending_wakes: Set[str] = set()
        self._retry_handles: Dict[str, asyncio.TimerHandle] = {}
        self._task_metadata: Dict[asyncio.Task, Tuple[str, int]] = {}
        self._task_failures: List[Tuple[int, BaseException]] = []
        self._next_task_sequence = 0
        self._closed = False

    def wake(self, endpoint: str) -> None:
        if self._closed:
            return
        current = self._endpoint_tasks.get(endpoint)
        if current is not None and not current.done():
            self._pending_wakes.add(endpoint)
            return

        retry = self._retry_handles.pop(endpoint, None)
        if retry is not None:
            retry.cancel()

        task = asyncio.create_task(self._run_endpoint(endpoint))
        self._endpoint_tasks[endpoint] = task
        self._task_metadata[task] = (endpoint, self._next_task_sequence)
        self._next_task_sequence += 1
        task.add_done_callback(self._task_done)

    async def recover(self) -> None:
        self.state.requeue_offered_messages(
            "dispatcher startup found persisted offered delivery"
        )
        self.state.reconcile_incomplete_messages(
            RECOVERABLE_ADAPTERS,
            "dispatcher startup found persisted accepted delivery"
        )
        for endpoint in self.state.list_dispatchable_endpoints():
            self.wake(endpoint)

    async def drain(self) -> None:
        while self._endpoint_tasks:
            tasks = tuple(self._endpoint_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                self._task_done(task)
        self._raise_next_failure()

    async def close(self) -> None:
        self._closed = True
        for handle in self._retry_handles.values():
            handle.cancel()
        self._retry_handles.clear()
        self._pending_wakes.clear()
        tasks = tuple(self._endpoint_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            for task in tasks:
                self._task_done(task)
        self._endpoint_tasks.clear()
        self.state.requeue_offered_messages(
            "dispatcher shutdown left offered delivery"
        )
        self.state.reconcile_incomplete_messages(
            RECOVERABLE_ADAPTERS,
            "dispatcher shutdown left accepted delivery"
        )

    async def _run_endpoint(self, endpoint: str) -> None:
        while True:
            lease_id = self.lease_id_factory()
            claimed = self.state.claim_next(endpoint, lease_id, time.time())
            if claimed is None:
                return

            try:
                descriptor = self.registry.resolve(endpoint, endpoint.partition("/")[0])
            except EndpointNotFound:
                waiting = self.state.transition_core_message(
                    claimed["message_id"],
                    (CoreMessageState.OFFERED,),
                    CoreMessageState.WAITING_ENDPOINT,
                    "endpoint not registered",
                    lease_id=lease_id,
                )
                if not waiting:
                    return
                return

            if descriptor.status == "offline":
                waiting = self.state.transition_core_message(
                    claimed["message_id"],
                    (CoreMessageState.OFFERED,),
                    CoreMessageState.WAITING_ENDPOINT,
                    "endpoint offline",
                    lease_id=lease_id,
                )
                if not waiting:
                    return
                return

            if descriptor.status == "retired":
                failed = self.state.transition_core_message(
                    claimed["message_id"],
                    (CoreMessageState.OFFERED,),
                    CoreMessageState.FAILED,
                    "endpoint retired",
                    lease_id=lease_id,
                )
                if not failed:
                    return
                continue

            message = self._message_from_row(claimed, CoreMessageState.OFFERED)
            lease = DeliveryLease(
                message_id=claimed["message_id"],
                lease_id=lease_id,
                adapter=descriptor.adapter,
                accept_callback=partial(
                    self.state.accept_core_message,
                    claimed["message_id"],
                    lease_id,
                    descriptor.adapter,
                ),
                expires_at=claimed["lease_expires_at"],
            )
            clear_lease = False
            try:
                adapter = self.adapters[descriptor.adapter]
            except Exception as exc:
                next_state = CoreMessageState.FAILED
                reason = "adapter delivery failed: {0}".format(exc)
                result_attempts = None
                expected_states = (CoreMessageState.OFFERED,)
            else:
                result_attempts = None
                try:
                    result = await adapter.deliver(message, lease)
                    result_attempts = result.attempts
                    reason = result.reason
                    if result.state == AdapterDeliveryState.DEFERRED:
                        next_state = CoreMessageState.WAITING_ENDPOINT
                        expected_states = (
                            CoreMessageState.OFFERED,
                            CoreMessageState.ACCEPTED,
                        )
                        clear_lease = True
                    elif result.state == AdapterDeliveryState.FAILED:
                        next_state = CoreMessageState.FAILED
                        expected_states = (
                            (CoreMessageState.ACCEPTED,)
                            if lease.accepted
                            else (CoreMessageState.OFFERED,)
                        )
                    else:
                        next_state = {
                            AdapterDeliveryState.DELIVERED: CoreMessageState.DELIVERED,
                            AdapterDeliveryState.DELIVERY_UNKNOWN: CoreMessageState.DELIVERY_UNKNOWN,
                        }[result.state]
                        expected_states = (CoreMessageState.ACCEPTED,)
                except Exception as exc:
                    if lease.accepted:
                        next_state = CoreMessageState.DELIVERY_UNKNOWN
                        expected_states = (CoreMessageState.ACCEPTED,)
                    else:
                        next_state = CoreMessageState.FAILED
                        expected_states = (CoreMessageState.OFFERED,)
                    reason = "adapter delivery failed: {0}".format(exc)

            transitioned = self.state.transition_core_message(
                claimed["message_id"],
                expected_states,
                next_state,
                reason,
                lease_id=lease_id,
                attempts=result_attempts,
                clear_lease=clear_lease,
            )
            if not transitioned:
                raise RuntimeError(
                    "lost terminal transition ownership for message {0}".format(
                        claimed["message_id"]
                    )
                )
            if next_state == CoreMessageState.WAITING_ENDPOINT:
                if self._endpoint_is_available(endpoint):
                    self._schedule_retry(endpoint)
                return

    def _endpoint_is_available(self, endpoint: str) -> bool:
        try:
            descriptor = self.registry.resolve(endpoint, endpoint.partition("/")[0])
        except EndpointNotFound:
            return False
        return descriptor.status not in {"offline", "retired"}

    def _schedule_retry(self, endpoint: str) -> None:
        if self._closed or endpoint in self._retry_handles:
            return
        self._retry_handles[endpoint] = asyncio.get_running_loop().call_later(
            self.retry_delay_seconds,
            self._retry_ready,
            endpoint,
        )

    def _retry_ready(self, endpoint: str) -> None:
        self._retry_handles.pop(endpoint, None)
        self.wake(endpoint)

    def _task_done(self, task: asyncio.Task) -> None:
        metadata = self._task_metadata.pop(task, None)
        if metadata is None:
            return
        endpoint, sequence = metadata
        if self._endpoint_tasks.get(endpoint) is task:
            self._endpoint_tasks.pop(endpoint, None)
        if endpoint in self._pending_wakes and not self._closed:
            self._pending_wakes.remove(endpoint)
            self.wake(endpoint)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._task_failures.append((sequence, error))

    def _raise_next_failure(self) -> None:
        if not self._task_failures:
            return
        self._task_failures.sort(key=lambda failure: failure[0])
        _, error = self._task_failures.pop(0)
        raise error

    @staticmethod
    def _message_from_row(row, state: CoreMessageState) -> CoreMessage:
        return CoreMessage(
            message_id=row["message_id"],
            source_endpoint=row["source_endpoint"],
            target_endpoint=row["target_endpoint"],
            body=row["body"],
            raw_text=row["raw_text"],
            state=state,
            conversation_id=row["conversation_id"],
            reply_to=row["reply_to"],
            metadata=json.loads(row["metadata_json"]),
        )
