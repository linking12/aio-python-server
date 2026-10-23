from __future__ import annotations

import asyncio
import logging
import os
import time
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.core.env import get_env_int


logger = logging.getLogger(__name__)


def _get_env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off'}:
        return False
    logger.warning('Invalid %s=%r, using default %r', name, value, default)
    return default


@dataclass(frozen=True)
class LargeOutputPaths:
    root_dir: Path
    relative_path: str
    absolute_path: str
    temp_relative_path: str | None = None
    temp_absolute_path: str | None = None


class TruncatedOutputStore:
    _CLEANUP_FALLBACK_INTERVAL_SECONDS = 600
    _IN_PROGRESS_DIR_NAME = '.in_progress'
    _BOOT_MARKER_NAME = '.boot'

    def __init__(self) -> None:
        self.enabled = _get_env_bool('AIO_SAVE_TRUNCATED_OUTPUT_FILE_ENABLED', True)
        configured_dir = os.environ.get(
            'AIO_SAVE_TRUNCATED_OUTPUT_FILE_DIR', '/tmp/aio-sandbox-truncated-output'
        ).strip()
        self.root_dir = Path(configured_dir or '/tmp/aio-sandbox-truncated-output')
        self.ttl_seconds = get_env_int(
            'AIO_SAVE_TRUNCATED_OUTPUT_FILE_TTL_SECONDS', 3600, min_value=1
        )
        self.max_files = get_env_int(
            'AIO_SAVE_TRUNCATED_OUTPUT_FILE_MAX_FILES', 1000, min_value=1
        )
        self.cleanup_interval_seconds = get_env_int(
            'AIO_SAVE_TRUNCATED_OUTPUT_FILE_CLEANUP_INTERVAL_SECONDS',
            self._CLEANUP_FALLBACK_INTERVAL_SECONDS,
            min_value=1,
        )
        self.in_progress_ttl_seconds = get_env_int(
            'AIO_SAVE_TRUNCATED_OUTPUT_FILE_IN_PROGRESS_TTL_SECONDS',
            max(self.ttl_seconds, self.cleanup_interval_seconds * 2),
            min_value=1,
        )
        self.boot_id = uuid.uuid4().hex
        self._active_spill_temp_paths: set[Path] = set()
        self._cleanup_task: Optional[asyncio.Task] = None

    def build_path(self, domain: str, command_id: str, suffix: str) -> LargeOutputPaths:
        if domain not in {'shell', 'bash'}:
            raise ValueError(f'Unsupported large output domain: {domain!r}')

        relative_path = f'{domain}/{command_id}{suffix}'
        absolute_path = str(self.root_dir / relative_path)
        return LargeOutputPaths(
            root_dir=self.root_dir,
            relative_path=relative_path,
            absolute_path=absolute_path,
        )

    def build_spill_path(
        self, domain: str, command_id: str, suffix: str
    ) -> LargeOutputPaths:
        final_paths = self.build_path(domain, command_id, suffix)
        temp_relative_path = (
            f'{self._IN_PROGRESS_DIR_NAME}/{self.boot_id}/'
            f'{domain}/{command_id}{suffix}.tmp'
        )
        temp_absolute_path = str(self.root_dir / temp_relative_path)
        return LargeOutputPaths(
            root_dir=final_paths.root_dir,
            relative_path=final_paths.relative_path,
            absolute_path=final_paths.absolute_path,
            temp_relative_path=temp_relative_path,
            temp_absolute_path=temp_absolute_path,
        )

    async def persist_text(
        self,
        *,
        domain: str,
        command_id: str,
        content: str,
        suffix: str,
    ) -> Optional[str]:
        if not self.enabled or not content:
            return None

        paths = self.build_path(domain, command_id, suffix)
        await asyncio.to_thread(self._write_text_atomic, paths, content)
        # Enforce upper bound right after a write so a burst of large
        # outputs cannot grow the store far beyond max_files between
        # periodic cleanup ticks.
        try:
            await self.enforce_max_files()
        except Exception as exc:
            logger.warning('Failed to enforce max_files after persist: %s', exc)
        return paths.absolute_path

    async def acquire_spill_path(
        self, *, domain: str, command_id: str, suffix: str
    ) -> Optional[LargeOutputPaths]:
        """为流式落盘准备临时路径与最终路径，确保父目录已存在。

        与 ``persist_text`` 不同，调用方持有文件句柄并自行追加写入。
        若 store 未启用则返回 None，调用方据此跳过 spill。
        """
        if not self.enabled:
            return None
        paths = self.build_spill_path(domain, command_id, suffix)
        await asyncio.to_thread(self._ensure_parent_dirs, paths)
        self._register_active_spill(paths)
        return paths

    async def finalize_spill(
        self,
        paths: Optional[LargeOutputPaths],
        *,
        keep: bool,
        enforce_max_files: bool = True,
    ) -> Optional[str]:
        """关闭一次 spill 落盘，返回最终路径或 ``None``（被丢弃时）。

        - ``keep=True``：将临时文件原子移动到最终路径，并主动触发
          ``enforce_max_files``。
        - ``keep=False``：删除已写入的临时文件，避免无效落盘文件残留。
        """
        if paths is None:
            return None
        temp_path = self._get_spill_temp_path(paths)
        try:
            if not keep:
                await asyncio.to_thread(self._unlink_quiet, temp_path)
                return None
            await asyncio.to_thread(self._promote_spill_file, paths)
            if enforce_max_files:
                try:
                    await self.enforce_max_files()
                except Exception as exc:
                    logger.warning('Failed to enforce max_files after spill: %s', exc)
            return paths.absolute_path
        finally:
            self._unregister_active_spill(paths)

    def _ensure_parent_dir(self, paths: LargeOutputPaths) -> None:
        Path(paths.absolute_path).parent.mkdir(parents=True, exist_ok=True)

    def _ensure_parent_dirs(self, paths: LargeOutputPaths) -> None:
        self._ensure_parent_dir(paths)
        self._touch_boot_marker_sync()
        temp_path = self._get_spill_temp_path(paths)
        temp_path.parent.mkdir(parents=True, exist_ok=True)

    def _get_spill_temp_path(self, paths: LargeOutputPaths) -> Path:
        if not paths.temp_absolute_path:
            raise ValueError('Spill paths must include temp_absolute_path')
        return Path(paths.temp_absolute_path)

    def _register_active_spill(self, paths: LargeOutputPaths) -> None:
        self._active_spill_temp_paths.add(self._get_spill_temp_path(paths))

    def _unregister_active_spill(self, paths: LargeOutputPaths) -> None:
        self._active_spill_temp_paths.discard(self._get_spill_temp_path(paths))

    def _boot_dir(self) -> Path:
        return self.root_dir / self._IN_PROGRESS_DIR_NAME / self.boot_id

    def _boot_marker_path(self) -> Path:
        return self._boot_dir() / self._BOOT_MARKER_NAME

    def _touch_boot_marker_sync(self) -> None:
        marker = self._boot_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()

    def _unlink_quiet(self, target: Path) -> None:
        try:
            target.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning('Failed to remove spill file %s: %s', target, exc)

    def _promote_spill_file(self, paths: LargeOutputPaths) -> None:
        temp_path = self._get_spill_temp_path(paths)
        target = Path(paths.absolute_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(target)

    def _write_text_atomic(self, paths: LargeOutputPaths, content: str) -> None:
        target = Path(paths.absolute_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=target.parent,
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(target)

    async def cleanup_expired(self) -> int:
        if not self.root_dir.exists():
            return 0
        return await asyncio.to_thread(self._cleanup_expired_sync)

    def _cleanup_expired_sync(self) -> int:
        removed = 0
        import time

        current_time = time.time()
        for path in self._iter_large_output_files():
            try:
                if current_time - path.stat().st_mtime > self.ttl_seconds:
                    path.unlink(missing_ok=True)
                    removed += 1
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning('Failed to remove expired large output %s: %s', path, exc)
        return removed

    async def cleanup_stale_in_progress(self) -> int:
        if not self.root_dir.exists():
            return 0
        active_paths = set(self._active_spill_temp_paths)
        return await asyncio.to_thread(
            self._cleanup_stale_in_progress_sync, active_paths
        )

    def _cleanup_stale_in_progress_sync(self, active_paths: set[Path]) -> int:
        in_progress_root = self.root_dir / self._IN_PROGRESS_DIR_NAME
        if not in_progress_root.exists():
            return 0

        self._touch_boot_marker_sync()
        current_time = time.time()
        removed = 0
        for boot_dir in list(in_progress_root.iterdir()):
            if not boot_dir.is_dir():
                continue
            is_current_boot = boot_dir.name == self.boot_id
            if not is_current_boot and not self._boot_dir_is_stale(
                boot_dir, current_time
            ):
                continue
            for path in boot_dir.rglob('*'):
                if not path.is_file() or path.name == self._BOOT_MARKER_NAME:
                    continue
                if path in active_paths:
                    continue
                try:
                    if current_time - path.stat().st_mtime <= self.in_progress_ttl_seconds:
                        continue
                    path.unlink(missing_ok=True)
                    removed += 1
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning('Failed to remove stale spill temp %s: %s', path, exc)
            if not is_current_boot:
                if not self._boot_dir_has_temp_files(boot_dir):
                    self._unlink_quiet(boot_dir / self._BOOT_MARKER_NAME)
                self._remove_empty_dirs(boot_dir)
        return removed

    def _boot_dir_has_temp_files(self, boot_dir: Path) -> bool:
        return any(
            path.is_file() and path.name != self._BOOT_MARKER_NAME
            for path in boot_dir.rglob('*')
        )

    def _boot_dir_is_stale(self, boot_dir: Path, current_time: float) -> bool:
        marker = boot_dir / self._BOOT_MARKER_NAME
        try:
            marker_mtime = marker.stat().st_mtime
        except FileNotFoundError:
            return True
        except Exception as exc:
            logger.warning('Failed to stat spill boot marker %s: %s', marker, exc)
            return False
        return current_time - marker_mtime > self.in_progress_ttl_seconds

    def _remove_empty_dirs(self, root: Path) -> None:
        if not root.exists():
            return
        dirs = [path for path in root.rglob('*') if path.is_dir()]
        for path in sorted(dirs, key=lambda item: len(item.parts), reverse=True):
            try:
                path.rmdir()
            except OSError:
                pass
            except Exception as exc:
                logger.warning('Failed to remove empty spill dir %s: %s', path, exc)
        try:
            root.rmdir()
        except OSError:
            pass
        except Exception as exc:
            logger.warning('Failed to remove empty spill dir %s: %s', root, exc)

    async def enforce_max_files(self) -> int:
        if not self.root_dir.exists():
            return 0
        return await asyncio.to_thread(self._enforce_max_files_sync)

    def _enforce_max_files_sync(self) -> int:
        files = []
        for path in self._iter_large_output_files():
            try:
                files.append((path.stat().st_mtime, path))
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning('Failed to stat large output %s: %s', path, exc)

        if len(files) <= self.max_files:
            return 0

        files.sort(key=lambda item: item[0])
        to_remove = len(files) - self.max_files
        removed = 0
        for _, path in files[:to_remove]:
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except FileNotFoundError:
                continue
            except Exception as exc:
                logger.warning('Failed to trim large output %s: %s', path, exc)
        return removed

    def _iter_large_output_files(self) -> list[Path]:
        if not self.root_dir.exists():
            return []
        files: list[Path] = []
        for domain in ('shell', 'bash'):
            domain_dir = self.root_dir / domain
            if not domain_dir.exists():
                continue
            files.extend(
                path
                for path in domain_dir.rglob('*')
                if path.is_file() and path.name != '.gitkeep'
            )
        return files

    async def cleanup(self) -> None:
        expired = await self.cleanup_expired()
        trimmed = await self.enforce_max_files()
        stale_in_progress = await self.cleanup_stale_in_progress()
        if expired or trimmed or stale_in_progress:
            logger.info(
                'Large output cleanup finished: expired=%s trimmed=%s stale_in_progress=%s',
                expired,
                trimmed,
                stale_in_progress,
            )

    def start_cleanup_task(self) -> None:
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop_cleanup_task(self) -> None:
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        self._cleanup_task = None

    async def shutdown(self) -> None:
        self.stop_cleanup_task()
        await self.cleanup()

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                await self.cleanup()
                await asyncio.sleep(self.cleanup_interval_seconds)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning('Error during large output cleanup: %s', exc)
                await asyncio.sleep(self.cleanup_interval_seconds)
