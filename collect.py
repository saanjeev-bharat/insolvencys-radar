"""Daily job: collect notices, then write the website data file."""
import pipeline
from export_json import export

if __name__ == "__main__":
    print(f"collected {pipeline.run()} notices")
    print(f"exported {export()} leads")
