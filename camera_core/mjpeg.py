"""camera_core.mjpeg — отдача MJPEG, которая закрывает генератор зрителя вместе с соединением."""
from fastapi.responses import StreamingResponse

MEDIA_TYPE = "multipart/x-mixed-replace; boundary=frame"


class MjpegResponse(StreamingResponse):
    """StreamingResponse сам не закрывает синхронный генератор, когда браузер ушёл: тот остаётся «приостановленным» до сборки
    мусора, а с ним и счётчик зрителей — камера продолжала декодироваться и кодироваться в JPEG после «глазика» и закрытой
    вкладки. Здесь генератор закрывается сразу, как только ответ кончился (его finally отрабатывает тут же)."""

    def __init__(self, frames, **kwargs):
        kwargs.setdefault("media_type", MEDIA_TYPE)
        super().__init__(frames, **kwargs)
        self._frames = frames

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._frames.close()
