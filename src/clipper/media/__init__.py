"""Media processing: download, transcription, captions and rendering."""


class MediaError(Exception):
    """Raised when a media step (download, ffmpeg, whisper) fails."""
