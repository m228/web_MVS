"""camera_core.imaging — преобразование кадров (Bayer/RGB → BGR), хостовая цветокоррекция.

Часть бывшего camera_core.py (механический перенос, тела определений не менялись).
"""
import numpy as np
import cv2


# GenICam Bayer-формат -> код дебайеринга OpenCV. ВНИМАНИЕ: OpenCV именует
# Байер-паттерн от ВТОРОГО пикселя строки/столбца, поэтому имена «перевёрнуты»
# относительно GenICam (GenICam BayerRG соответствует OpenCV BayerBG2BGR и т.д.).
# Если после перехода на Bayer красный и синий поменяны местами — поставьте
# соседний код из этой таблицы (RG<->BG, GR<->GB).
_BAYER_TO_BGR = {
    "BayerRG": cv2.COLOR_BayerBG2BGR,
    "BayerGB": cv2.COLOR_BayerGR2BGR,
    "BayerGR": cv2.COLOR_BayerGB2BGR,
    "BayerBG": cv2.COLOR_BayerRG2BGR,
}


def _bayer_code(pixel_format):
    if not pixel_format:
        return None
    for key, code in _BAYER_TO_BGR.items():
        if str(pixel_format).startswith(key):
            return code
    return None


# разбор сырого буфера кадра в BGR-картинку c учётом пиксельного формата.
# Число каналов определяем по размеру буфера: 3 (RGB/BGR), 1 (Mono/Bayer),
# 4 (RGBA). Раньше код жёстко решейпил в 3 канала и падал на моно-камере
# (ValueError: cannot reshape array of size ... into (h, w, 3)).
def _to_bgr(data, width, height, pixel_format=None):
    arr = np.asarray(data, dtype=np.uint8).reshape(-1)
    pixels = width * height
    if pixels <= 0 or arr.size == 0 or arr.size % pixels != 0:
        return None

    channels = arr.size // pixels

    if channels == 1:
        # 1 байт/пиксель: Bayer -> цвет (дебайеринг), иначе Mono -> серое.
        # Bayer втрое легче RGB8 по трафику — рекомендуемый формат для GigE.
        bayer = _bayer_code(pixel_format)
        if bayer is not None:
            return cv2.cvtColor(arr.reshape(height, width), bayer)
        return cv2.cvtColor(arr.reshape(height, width), cv2.COLOR_GRAY2BGR)

    if channels == 3:
        frame = arr.reshape(height, width, 3)
        # продюсер отдаёт RGB8 -> переводим в BGR (иначе R и B перепутаны в OpenCV).
        # Формат неизвестен -> оставляем как есть (прежнее поведение).
        if pixel_format and str(pixel_format).upper().startswith("RGB"):
            return cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return frame

    if channels == 4:
        return cv2.cvtColor(arr.reshape(height, width, 4), cv2.COLOR_RGBA2BGR)

    return None


# ---- Хостовая цветокоррекция (гибрид «как в MVS»): гамма/насыщенность/оттенок/
# контраст/яркость/CCM/псевдоцвет над BGR-кадром ПОСЛЕ дебайера. Баланс белого/Gain/
# BlackLevel делает камера (GenICam) — они в Bayer работают, тут их нет. ----
_GAMMA_LUT_CACHE = {}

def _gamma_lut(g):
    key = round(float(g), 3)
    lut = _GAMMA_LUT_CACHE.get(key)
    if lut is None:
        inv = 1.0 / max(0.01, key)
        lut = np.clip((np.arange(256) / 255.0) ** inv * 255.0, 0, 255).astype(np.uint8)
        _GAMMA_LUT_CACHE[key] = lut
    return lut

# доступные псевдоцвет-палитры (имя из UI -> COLORMAP OpenCV)
_COLORMAPS = {
    "jet": cv2.COLORMAP_JET, "hot": cv2.COLORMAP_HOT, "turbo": cv2.COLORMAP_TURBO,
    "viridis": cv2.COLORMAP_VIRIDIS, "magma": cv2.COLORMAP_MAGMA, "bone": cv2.COLORMAP_BONE,
    "ocean": cv2.COLORMAP_OCEAN, "hsv": cv2.COLORMAP_HSV, "rainbow": cv2.COLORMAP_RAINBOW,
}


def _apply_color(img, c):
    """Применить цветокоррекцию к BGR-кадру. c — dict; пустой/None = без изменений.
    Порядок: контраст/яркость -> гамма -> CCM -> насыщенность/оттенок -> палитра."""
    if not c or img is None:
        return img
    try:
        def _f(v, d):   # None-безопасно: 0.0 — валидное значение (напр. saturation=0), не путать с «нет»
            return d if v is None else float(v)
        gamma = _f(c.get("gamma"), 1.0)
        sat = _f(c.get("saturation"), 1.0)
        hue = _f(c.get("hue"), 0.0)
        contrast = _f(c.get("contrast"), 1.0)
        bright = _f(c.get("brightness"), 0.0)
        sharpness = _f(c.get("sharpness"), 0.0)   # 0 = без резкости; 0..2 сила unsharp mask
        clarity = _f(c.get("clarity"), 0.0)       # 0 = выкл; локальный контраст (CLAHE clipLimit)
        denoise = _f(c.get("denoise"), 0.0)       # 0 = выкл; сила шумоподавления (bilateral)
        ccm = c.get("ccm")
        palette = c.get("palette") or ""
        wb = c.get("wb")   # {"auto":1} | {"r":g,"g":g,"b":g} (гейны каналов)

        # шумоподавление ПЕРВЫМ (чистим зерно до усиления контраста/резкости, иначе шум усилится).
        # bilateral сохраняет грани; сила 0..10 -> sigmaColor/Space.
        if denoise > 0.5:
            s = float(denoise) * 12.0
            img = cv2.bilateralFilter(img, d=5, sigmaColor=s, sigmaSpace=s)

        # баланс белого (на хосте): авто «серый мир» ИЛИ ручные гейны R/G/B.
        # img в BGR-порядке каналов (0=B,1=G,2=R).
        if wb:
            if wb.get("auto"):
                means = img.reshape(-1, 3).mean(axis=0) + 1e-6
                gray = float(means.mean())
                gains = gray / means   # [gB, gG, gR]
                img = np.clip(img.astype(np.float32) * gains, 0, 255).astype(np.uint8)
            else:
                gains = np.array([float(wb.get("b", 1.0)),
                                  float(wb.get("g", 1.0)),
                                  float(wb.get("r", 1.0))], dtype=np.float32)
                if np.any(np.abs(gains - 1.0) > 1e-3):
                    img = np.clip(img.astype(np.float32) * gains, 0, 255).astype(np.uint8)

        if abs(contrast - 1.0) > 1e-3 or abs(bright) > 1e-3:
            img = cv2.convertScaleAbs(img, alpha=contrast, beta=bright)
        if abs(gamma - 1.0) > 1e-3:
            img = cv2.LUT(img, _gamma_lut(gamma))
        if ccm:
            try:
                M = np.asarray(ccm, dtype=np.float32).reshape(3, 3)
                img = cv2.transform(img, M)
            except Exception:
                pass
        if abs(sat - 1.0) > 1e-3 or abs(hue) > 1e-3:
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
            if abs(hue) > 1e-3:
                hsv[..., 0] = (hsv[..., 0] + hue / 2.0) % 180.0   # OpenCV H: 0..179
            if abs(sat - 1.0) > 1e-3:
                hsv[..., 1] = np.clip(hsv[..., 1] * sat, 0, 255)
            img = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)
        # резкость (unsharp mask): img + amount*(img - размытие). Ставим до палитры,
        # чтобы контуры подчёркивались на реальном кадре, а не на псевдоцвете.
        if sharpness > 0.01:
            blur = cv2.GaussianBlur(img, (0, 0), 3)
            img = cv2.addWeighted(img, 1.0 + sharpness, blur, -sharpness, 0)
        # локальный контраст (CLAHE) по L-каналу LAB — «проявляет» структуру/грани кристаллов
        # без ореолов unsharp. clarity = clipLimit; до палитры.
        if clarity > 0.05:
            lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
            l, a, b = cv2.split(lab)
            clahe = cv2.createCLAHE(clipLimit=float(clarity), tileGridSize=(8, 8))
            l = clahe.apply(l)
            img = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
        if palette:
            cm = _COLORMAPS.get(palette)
            if cm is not None:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                img = cv2.applyColorMap(gray, cm)
        return img
    except Exception:
        return img
