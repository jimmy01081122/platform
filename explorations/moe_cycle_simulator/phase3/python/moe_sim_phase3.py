"""Thin ctypes binding for the sole C++20 Phase 3 scheduling core."""
from __future__ import annotations

import ctypes
from pathlib import Path
from typing import Iterable


class EngineError(RuntimeError):
    pass


class Engine:
    def __init__(
        self,
        library: Path,
        *,
        phase2_ledger_sha256: str,
        canonical_bundle_semantic_root: str,
        engine_build_sha256: str,
        engine_profile_sha256: str,
        checkpoint_schema_sha256: str,
        max_events: int = 1_000_000,
        max_same_time_events: int = 100_000,
        max_wait_for_nodes: int = 100_000,
    ) -> None:
        self._library = ctypes.CDLL(str(library))
        self._bind()
        error = ctypes.create_string_buffer(1024)
        self._handle = self._library.moe_phase3_engine_create(
            max_events,
            max_same_time_events,
            max_wait_for_nodes,
            phase2_ledger_sha256.encode(),
            canonical_bundle_semantic_root.encode(),
            engine_build_sha256.encode(),
            engine_profile_sha256.encode(),
            checkpoint_schema_sha256.encode(),
            error,
            len(error),
        )
        if not self._handle:
            raise EngineError(error.value.decode())

    def _bind(self) -> None:
        library = self._library
        library.moe_phase3_engine_create.restype = ctypes.c_void_p
        library.moe_phase3_engine_create.argtypes = [
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_uint64,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        library.moe_phase3_engine_destroy.argtypes = [ctypes.c_void_p]
        library.moe_phase3_engine_add_resource.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        library.moe_phase3_engine_add_event.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_int,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.c_size_t,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        library.moe_phase3_engine_finalize.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        library.moe_phase3_engine_run.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        library.moe_phase3_engine_state_digest.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

    def _check(self, result: int, error: ctypes.Array[ctypes.c_char]) -> None:
        if result != 0:
            raise EngineError(error.value.decode())

    def add_resource(
        self, resource_id: str, capacity: int, *,
        resource_kind: int = 0, arbitration: int = 0
    ) -> None:
        error = ctypes.create_string_buffer(1024)
        result = self._library.moe_phase3_engine_add_resource(
            self._handle,
            resource_id.encode(),
            resource_kind,
            arbitration,
            str(capacity).encode(),
            error,
            len(error),
        )
        self._check(result, error)

    def add_event(
        self, event_id: str, time_fs: int, priority: int, action: int,
        resource_id: str, owner_id: str, *,
        dependencies: Iterable[str] = (),
        request_id: str | None = "request-1",
        token_index: int | None = 0,
        layer_index: int | None = 0,
        component_id: str = "gpu0",
        quantity: int = 1,
        service_demand: int = 0,
    ) -> None:
        encoded_dependencies = [item.encode() for item in dependencies]
        dependency_array = (ctypes.c_char_p * len(encoded_dependencies))(
            *encoded_dependencies
        )
        error = ctypes.create_string_buffer(1024)
        result = self._library.moe_phase3_engine_add_event(
            self._handle,
            event_id.encode(),
            str(time_fs).encode(),
            priority,
            None if request_id is None else request_id.encode(),
            token_index is not None,
            0 if token_index is None else token_index,
            layer_index is not None,
            0 if layer_index is None else layer_index,
            component_id.encode(),
            dependency_array,
            len(encoded_dependencies),
            action,
            resource_id.encode(),
            owner_id.encode(),
            str(quantity).encode(),
            str(service_demand).encode(),
            error,
            len(error),
        )
        self._check(result, error)

    def finalize(self) -> None:
        error = ctypes.create_string_buffer(1024)
        self._check(
            self._library.moe_phase3_engine_finalize(
                self._handle, error, len(error)
            ),
            error,
        )

    def run(self) -> int:
        status = ctypes.c_int()
        error = ctypes.create_string_buffer(1024)
        self._check(
            self._library.moe_phase3_engine_run(
                self._handle, ctypes.byref(status), error, len(error)
            ),
            error,
        )
        return status.value

    def state_digest(self) -> str:
        output = ctypes.create_string_buffer(128)
        error = ctypes.create_string_buffer(1024)
        self._check(
            self._library.moe_phase3_engine_state_digest(
                self._handle, output, len(output), error, len(error)
            ),
            error,
        )
        return output.value.decode()

    def close(self) -> None:
        if self._handle:
            self._library.moe_phase3_engine_destroy(self._handle)
            self._handle = None

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
