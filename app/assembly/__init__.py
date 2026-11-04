"""Local assembly: ffmpeg/ffprobe wrapper (1.8), manifest-driven stitch + image export (1.12).

Modules:
- ``ffmpeg``   — subprocess wrapper + ffprobe (BUILD_PLAN 1.8); every invocation
  in this package goes through it, never raw subprocess.
- ``video``    — timing-manifest-driven stitch: H.264/AAC MP4 with
  fps/resolution/aspect normalization (task 1.12).
- ``images``   — platform-correct PNG/JPEG export with optional drawtext overlay.
- ``pipeline`` — job → deliverables orchestration: probed audio durations →
  timing manifest → stitch/export into ``output/`` (§6.2–6.4, §7 layout).
"""


class AssemblyError(ValueError):
    """A local assembly step failed for a reason the user can act on.

    Raised by the stitch, the image export, and the pipeline orchestration:
    the message names the beat/file/setting involved (§6.5 spirit — never a
    bare traceback). It subclasses ``ValueError`` so the HTTP layer's existing
    ``except ValueError`` → 409 mapping treats it as a client-visible outcome.
    """
