"""Дельта-обновление: докачать из архива релиза только изменившиеся файлы.

GitHub отдаёт asset с HTTP Range, поэтому у zip можно прочитать только оглавление (CRC32 и размер каждого файла), сравнить
с установленными файлами и Range-запросами забрать лишь отличающиеся. Остальное берётся с диска. На выходе — ПОЛНЫЙ zip,
такой же по содержимому, как архив релиза (неизменённые файлы лежат без сжатия), так что дальше всё идёт как при обычной
загрузке: распаковка, apply, update.ps1 -ZipPath.

Любой сбой (сервер без Range, обрыв, не сошлась сумма) — исключение: вызывающий откатывается на полную загрузку.
Только стандартная библиотека.
"""
from __future__ import annotations

import io
import os
import shutil
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from collections import OrderedDict
from pathlib import Path

BLOCK = 256 * 1024              # блоки по 256 КБ: мелкий файл не тянет за собой мегабайт, а на каждый кусок zipfile HTTP-запрос не делаем
CACHE_BLOCKS = 8
ATTEMPTS = 3
UA = {"User-Agent": "web_MVS-update"}


class DeltaError(Exception):
    """Дельта невозможна или не получилась — вызывающий качает архив целиком."""


class HttpRangeFile(io.RawIOBase):
    """Только для чтения файл поверх HTTP Range (для zipfile.ZipFile)."""

    def __init__(self, url: str, size: int, deadline: float = 0.0, timeout: float = 30.0):
        super().__init__()
        self.url0, self.url = url, url
        self.size = int(size)
        self.pos = 0
        self.deadline = deadline
        self.timeout = timeout
        self.fetched = 0                          # сколько байт реально пришло по сети
        self.requests = 0
        self._cache: OrderedDict = OrderedDict()

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = {0: offset, 1: self.pos + offset, 2: self.size + offset}[whence]
        self.pos = max(0, self.pos)
        return self.pos

    def _fetch(self, i: int) -> bytes:
        start = i * BLOCK
        end = min(self.size, start + BLOCK) - 1
        last = None
        for attempt in range(ATTEMPTS):
            if self.deadline and time.time() > self.deadline:
                raise DeltaError("превышено общее время скачивания")
            req = urllib.request.Request(self.url, headers=dict(UA, Range="bytes=%d-%d" % (start, end)))
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    if resp.status != 206:
                        raise DeltaError("сервер не поддерживает Range (HTTP %s)" % resp.status)
                    data = resp.read()
                    self.url = resp.geturl()      # подписанный адрес хранилища: следующие блоки идут без редиректа через GitHub
                if len(data) != end - start + 1:
                    raise OSError("блок %d: получено %d из %d байт" % (i, len(data), end - start + 1))
                self.fetched += len(data)
                self.requests += 1
                return data
            except DeltaError:
                raise
            except (OSError, urllib.error.URLError) as exc:      # HTTPError — тоже OSError
                last = exc
                self.url = self.url0                  # подписанный адрес мог протухнуть — начнём заново через GitHub
                time.sleep(1.0 * (attempt + 1))
        raise DeltaError("не удалось прочитать блок %d: %s" % (i, last))

    def _block(self, i: int) -> bytes:
        b = self._cache.get(i)
        if b is None:
            b = self._fetch(i)
            self._cache[i] = b
            while len(self._cache) > CACHE_BLOCKS:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(i)
        return b

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while n > 0:
            i, off = divmod(self.pos, BLOCK)
            chunk = self._block(i)[off:off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def readinto(self, b):
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)


def _crc32_file(path: Path) -> int:
    crc = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _local_path(app_dir: Path, name: str) -> Path | None:
    """Путь установленного файла для записи архива; None — если имя уводит за пределы папки установки."""
    p = (app_dir / name).resolve()
    root = app_dir.resolve()
    return p if root in p.parents else None


def plan(zf: zipfile.ZipFile, app_dir: Path):
    """(same, fetch): записи, чей установленный файл совпал по размеру и CRC32, и записи, которые надо докачать."""
    same, fetch = [], []
    for info in zf.infolist():
        if info.is_dir():
            continue
        local = _local_path(app_dir, info.filename)
        try:
            ok = local is not None and local.is_file() and local.stat().st_size == info.file_size and _crc32_file(local) == info.CRC
        except OSError:
            ok = False
        (same if ok else fetch).append(info)
    return same, fetch


def fetch_delta(url: str, size: int, app_dir: Path, out_path: Path, deadline: float = 0.0, min_gain: float = 0.3) -> dict:
    """Собрать в out_path полный zip релиза: изменившиеся файлы — по сети (Range), остальные — из app_dir.

    min_gain — минимальная доля экономии трафика, иначе смысла нет (DeltaError, качаем целиком).
    Возвращает статистику: {"total", "fetched", "files", "changed", "requests"} (байты — в сжатом виде).
    """
    if size <= 0:
        raise DeltaError("неизвестен размер архива")
    rf = HttpRangeFile(url, size, deadline)
    try:
        try:
            zf = zipfile.ZipFile(rf)
        except zipfile.BadZipFile as exc:
            raise DeltaError("не удалось прочитать оглавление архива: %s" % exc)
        with zf:
            same, fetch = plan(zf, app_dir)
            total = sum(i.compress_size for i in zf.infolist())
            need = sum(i.compress_size for i in fetch)
            if total <= 0 or need > total * (1.0 - min_gain):
                raise DeltaError("выигрыш мал (нужно %d из %d байт) — проще скачать целиком" % (need, total))
            fetch_names = {i.filename for i in fetch}
            with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as out:
                for info in zf.infolist():
                    if info.is_dir():
                        out.writestr(info.filename, b"")
                        continue
                    zi = zipfile.ZipInfo(info.filename, info.date_time)
                    zi.external_attr = info.external_attr
                    if info.filename in fetch_names:
                        zi.compress_type = zipfile.ZIP_DEFLATED
                        with zf.open(info) as src, out.open(zi, "w") as dst:      # CRC32 сверяет zipfile на конце чтения
                            shutil.copyfileobj(src, dst, 1 << 20)
                    else:
                        zi.compress_type = zipfile.ZIP_STORED
                        local = _local_path(app_dir, info.filename)
                        with open(local, "rb") as src, out.open(zi, "w") as dst:
                            shutil.copyfileobj(src, dst, 1 << 20)
                    got = out.getinfo(info.filename)
                    if got.CRC != info.CRC or got.file_size != info.file_size:
                        raise DeltaError("файл %s не совпал с оглавлением релиза" % info.filename)
            return {"total": total, "fetched": rf.fetched, "files": len(same) + len(fetch), "changed": len(fetch), "requests": rf.requests}
    except DeltaError:
        _unlink(out_path)
        raise
    except Exception as exc:
        _unlink(out_path)
        raise DeltaError(str(exc))
    finally:
        rf.close()


def _unlink(path: Path):
    try:
        os.unlink(path)
    except OSError:
        pass
