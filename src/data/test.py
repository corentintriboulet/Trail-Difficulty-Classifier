from pathlib import Path
import yaml
from load_data import StravaSegmentExtractor   # <--- update name!

def main():
    # Load config to get token (if your class requires it)
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "config.yaml"

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    ACCESS_TOKEN = config['strava']['access_token']

    extractor = StravaSegmentExtractor(ACCESS_TOKEN)

    total = extractor.number_of_processed_segments()

    print(f"Total processed segments: {total}")



if __name__ == "__main__":
    main()
