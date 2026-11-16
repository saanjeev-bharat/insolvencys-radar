import asic_stats
import db
from pipeline import import_asic_stats


def main():
    print("Starting ASIC historical workbook download and read...", flush=True)
    appointments, info = asic_stats.load_all(months=None)
    print(
        f"ASIC workbook loaded: {len(appointments)} appointment rows. "
        "Connecting to Neon now...",
        flush=True,
    )

    with db.connect() as con:
        print("Neon connection established. Importing appointment history...", flush=True)
        count = import_asic_stats(con, appointments=appointments, info=info)

    print(
        f"ASIC appointment history import completed: {count} company summaries updated.",
        flush=True,
    )


if __name__ == "__main__":
    main()
