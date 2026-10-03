"""Remote GPU worker node.

Modules
-------
``daemon``        entry point, argument parsing, signal handling
``listener``      accept loop, thread-per-connection
``session``       per-connection protocol state machine
``job_queue``     bounded queue plus the fixed-size worker pool
``capabilities``  ffmpeg / GPU / CUDA probing
``storage``       on-disk job staging and checksum verification
``engines``       FFmpeg-NVENC and CUDA compute backends
"""

__all__ = ["capabilities", "daemon", "engines", "job_queue", "listener", "session", "storage"]