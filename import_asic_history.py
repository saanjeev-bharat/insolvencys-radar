import db
from pipeline import import_asic_stats

def main():
    with db.connect() as con:
        count = import_asic_stats(con)
    print(f"ASIC appointment history import completed: {count} company summaries updated.")

if __name__ == "__main__":
    main()
