"""Отчёт «день / ночь» по видимости палок из копии данных сервиса КЗ (папка с motion.db, backups, shots).

  python scripts/autocal_day_night_report.py E:\\grib_dif rtsp_3 --days 10 --out report_valdown.html
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "motion_service"))

import motion_autocal_report as rep  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("data_dir", help="папка data сервиса КЗ (или её копия)")
    ap.add_argument("camera", help="id камеры, например rtsp_3")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--no-shots", action="store_true", help="не прогонять снимки (быстрее)")
    ap.add_argument("--shots", type=int, default=150, help="сколько дневных и сколько ночных снимков прогнать (больше — точнее и дольше)")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    out = rep.build_report(a.data_dir, a.camera, days=a.days, shots=not a.no_shots, shots_per_part=a.shots)
    path = Path(a.out or ("autocal_day_night_%s.html" % a.camera))
    path.write_text(out, encoding="utf-8")
    print(path.resolve())


if __name__ == "__main__":
    main()
