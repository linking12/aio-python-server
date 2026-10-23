"""
Pipe-based Bash service data models.

Process-per-command model: each exec spawns a new process.
Env and cwd are isolated per command — use `env` param and `exec_dir` for control.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import IO, Any, Optional

from pydantic import BaseModel, Field


COMMAND_STATUS_DESCRIPTION = (
    'Command execution status. Possible values: '
    '`pending` (accepted but not started yet; typically internal-only), '
    '`running` (still executing), '
    '`completed` (process exited and `exit_code` is available; a non-zero shell exit code still uses `completed`), '
    '`timed_out` (process was force-killed after `hard_timeout`), '
    '`killed` (process was terminated by kill/cleanup or an internal execution failure).'
)

SESSION_STATUS_DESCRIPTION = (
    'Session status. Possible values: '
    '`ready` (session can accept commands), '
    '`closed` (session is closed and cannot accept new commands).'
)


class CommandStatus(str, Enum):
    """Status of a bash command execution."""

    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'
    TIMED_OUT = 'timed_out'
    KILLED = 'killed'


class SessionStatus(str, Enum):
    """Status of a pipe bash session."""

    READY = 'ready'
    CLOSED = 'closed'


@dataclass
class StreamSpillState:
    """单个流的有界内存 buffer + 流式落盘状态。

    - ``pre_spill`` 仅保留 ``limit + 1`` 字节以内的数据；一旦超过 limit
      就会被一次性 flush 到 ``handle`` 并清空，后续 chunk 直接写入文件，
      避免无界内存累积导致 OOM。
    - ``total_bytes`` 记录原始流的累计字节数，用于判断是否需要保留落盘
      文件。
    - ``paths``/``handle`` 在首次触发 spill 时由读取协程惰性创建。
    """

    pre_spill: bytearray = field(default_factory=bytearray, repr=False)
    total_bytes: int = 0
    paths: Optional[Any] = field(default=None, repr=False)
    handle: Optional[IO[bytes]] = field(default=None, repr=False)
    persisted_path: Optional[str] = None


@dataclass
class BashCommand:
    """Tracks a single command execution within a session."""

    id: str
    command: str
    status: CommandStatus = CommandStatus.PENDING
    exit_code: Optional[int] = None
    proc: Optional[asyncio.subprocess.Process] = field(default=None, repr=False)
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Physical buffer offsets at command start (set inside _exec_lock)
    stdout_start: int = 0
    stderr_start: int = 0
    # Truncation limit captured at exec time; 0 means no inline truncation.
    max_output_length: int = 0
    # 流式 spill 状态：超过 ``max_output_length`` 后会把内存 buffer 倾倒
    # 到磁盘文件并继续以追加模式写入，避免大输出时 Python 进程 OOM。
    stdout_spill: StreamSpillState = field(
        default_factory=StreamSpillState, repr=False
    )
    stderr_spill: StreamSpillState = field(
        default_factory=StreamSpillState, repr=False
    )

    @property
    def stdout_full_output_file_path(self) -> Optional[str]:
        return self.stdout_spill.persisted_path

    @property
    def stderr_full_output_file_path(self) -> Optional[str]:
        return self.stderr_spill.persisted_path


class BashCommandInfo(BaseModel):
    """Serializable command status info (for API responses)."""

    command_id: str = Field(..., description='Unique command identifier')
    command: str = Field(..., description='The command string')
    status: CommandStatus = Field(..., description=COMMAND_STATUS_DESCRIPTION)
    exit_code: Optional[int] = Field(None, description='Exit code (when completed)')
    stdout_full_output_file_path: Optional[str] = Field(
        None,
        description='Path to the full stdout file when the inline output is truncated',
    )
    stderr_full_output_file_path: Optional[str] = Field(
        None,
        description='Path to the full stderr file when the inline output is truncated',
    )


class BashExecResult(BaseModel):
    """Result of exec_command — returned by POST /v1/bash/exec."""

    session_id: str = Field(..., description='Session identifier')
    command_id: str = Field(..., description='Unique command identifier')
    command: str = Field(..., description='The executed command')
    status: CommandStatus = Field(..., description=COMMAND_STATUS_DESCRIPTION)
    stdout: Optional[str] = Field(None, description='Stdout output up to this point')
    stderr: Optional[str] = Field(None, description='Stderr output up to this point')
    exit_code: Optional[int] = Field(None, description='Exit code (when completed)')
    offset: int = Field(0, description='Current stdout offset for subsequent /output calls')
    stderr_offset: int = Field(0, description='Current stderr offset')
    stdout_full_output_file_path: Optional[str] = Field(
        None,
        description='Path to the full stdout file when the inline output is truncated',
    )
    stderr_full_output_file_path: Optional[str] = Field(
        None,
        description='Path to the full stderr file when the inline output is truncated',
    )


class BashOutputResult(BaseModel):
    """Result of get_output — returned by POST /v1/bash/output."""

    session_id: str = Field(..., description='Session identifier')
    stdout: str = Field('', description='New stdout data since last offset')
    stderr: str = Field('', description='New stderr data since last offset')
    offset: int = Field(0, description='Current stdout offset (use for next request)')
    stderr_offset: int = Field(0, description='Current stderr offset (use for next request)')
    command: Optional[BashCommandInfo] = Field(
        None, description='Current or most recent command status'
    )
    stdout_full_output_file_path: Optional[str] = Field(
        None,
        description='Path to the full stdout file when the inline output is truncated',
    )
    stderr_full_output_file_path: Optional[str] = Field(
        None,
        description='Path to the full stderr file when the inline output is truncated',
    )


class BashSessionInfo(BaseModel):
    """Session info for listing — returned by GET /v1/bash/sessions."""

    session_id: str = Field(..., description='Session identifier')
    status: SessionStatus = Field(..., description=SESSION_STATUS_DESCRIPTION)
    working_dir: str = Field(..., description='Working directory')
    created_at: datetime = Field(..., description='Creation timestamp')
    last_used_at: datetime = Field(..., description='Last used timestamp')
    current_command: Optional[str] = Field(
        None, description='Currently executing command'
    )
    command_count: int = Field(0, description='Total commands executed')
