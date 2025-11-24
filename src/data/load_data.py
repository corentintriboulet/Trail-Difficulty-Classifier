from pathlib import Path
import requests
import json
import time
import yaml
import os
import csv
import asyncio
import agentql
from playwright.async_api import async_playwright


class RateLimitException(Exception):
    """Raised when Strava API rate limit is hit"""
    pass


def refresh_strava_token(config_path: Path) -> str:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    strava = config['strava']
    response = requests.post(
        "https://www.strava.com/oauth/token",
        data={
            "client_id": strava['client_id'],
            "client_secret": strava['client_secret'],
            "grant_type": "refresh_token",
            "refresh_token": strava['refresh_token']
        }
    )
    
    if response.status_code == 200:
        tokens = response.json()
        config['strava']['access_token'] = tokens['access_token']
        config['strava']['refresh_token'] = tokens['refresh_token']
        with open(config_path, 'w') as f:
            yaml.dump(config, f, default_flow_style=False)
        print("✓ Token refreshed!")
        return tokens['access_token']
    print(f"✗ Token refresh failed: {response.text}")
    return None


def get_valid_token(config_path: Path) -> str:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    access_token = config['strava']['access_token']
    response = requests.get(
        "https://www.strava.com/api/v3/athlete",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    
    if response.status_code == 200:
        print("✓ Token is valid")
        return access_token
    elif response.status_code == 401:
        print("Token expired, refreshing...")
        return refresh_strava_token(config_path)
    print(f"✗ Unknown error: {response.status_code}")
    return None


class StravaSegmentExtractor:
    def __init__(self, access_token):
        self.access_token = access_token
        self.base_url = "https://www.strava.com/api/v3"
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.project_root = Path(__file__).resolve().parents[2]
        self.raw_folder = self.project_root / "data" / "raw"
        os.makedirs(self.raw_folder, exist_ok=True)
        
        self.browser = None
        self.playwright = None
        
        # Load segments without leaderboard
        self.no_leaderboard_file = self.raw_folder / "segments_no_leaderboard.json"
        self.no_leaderboard_ids = self._load_no_leaderboard_ids()
    
    def _load_no_leaderboard_ids(self) -> set:
        """Load IDs of segments known to have no leaderboard"""
        if self.no_leaderboard_file.exists():
            with open(self.no_leaderboard_file, 'r') as f:
                data = json.load(f)
                return set(data.get('segment_ids', []))
        return set()
    
    def _save_no_leaderboard_id(self, segment_id: int, segment_name: str = None):
        """Add a segment to the no-leaderboard list"""
        self.no_leaderboard_ids.add(segment_id)
        
        # Load existing data
        if self.no_leaderboard_file.exists():
            with open(self.no_leaderboard_file, 'r') as f:
                data = json.load(f)
        else:
            data = {'segment_ids': [], 'segments': []}
        
        # Add if not already there
        if segment_id not in data['segment_ids']:
            data['segment_ids'].append(segment_id)
            data['segments'].append({
                'id': segment_id,
                'name': segment_name,
                'checked_at': time.strftime('%Y-%m-%d %H:%M:%S')
            })
            
            with open(self.no_leaderboard_file, 'w') as f:
                json.dump(data, f, indent=2)
    
    def explore_segments(self, bounds, activity_type="riding"):
        url = f"{self.base_url}/segments/explore"
        params = {
            "bounds": ",".join(map(str, bounds)),
            "activity_type": activity_type
        }
        
        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            return response.json().get("segments", [])
        elif response.status_code == 429:
            print("Rate limited on explore, waiting 60s...")
            time.sleep(60)
            return self.explore_segments(bounds, activity_type)
        else:
            print(f"Error: {response.status_code} - {response.text}")
            return []
    
    def get_segment_details(self, segment_id):
        url = f"{self.base_url}/segments/{segment_id}"
        response = requests.get(url, headers=self.headers)
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            raise RateLimitException("Strava API rate limit reached (15min limit)")
        else:
            print(f"Error getting segment {segment_id}: {response.status_code}")
            return None
    
    def get_segment_streams(self, segment_id):
        url = f"{self.base_url}/segments/{segment_id}/streams"
        params = {"keys": "altitude,distance,latlng", "key_by_type": True}
        response = requests.get(url, headers=self.headers, params=params)
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            raise RateLimitException("Strava API rate limit reached (15min limit)")
        return None
    
    @staticmethod
    def time_to_seconds(time_str):
        try:
            parts = time_str.strip().split(':')
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            elif len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except:
            pass
        return None
    
    async def init_browser(self):
        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
    
    async def close_browser(self):
        if self.browser:
            await self.browser.close()
            await self.playwright.stop()
            self.browser = None
            self.playwright = None
    
    async def scrape_leaderboard_times(self, segment_id):
        """Scrape leaderboard using shared browser"""
        segment_url = f"https://www.strava.com/segments/{segment_id}"
        
        try:
            page = await agentql.wrap_async(await self.browser.new_page())
            await page.goto(segment_url)
            await page.wait_for_timeout(1500)
            
            query = """
            {
              table {
                row[] {
                  time
                }
              }
            }
            """
            
            leaderboard_data = await page.query_data(query)
            await page.close()
            
            rows = leaderboard_data.get("table", {}).get("row", [])
            times_str = [row.get("time") for row in rows if row.get("time")]
            
            times_seconds = [self.time_to_seconds(t) for t in times_str]
            times_seconds = [t for t in times_seconds if t is not None]
            
            if not times_seconds:
                return None, None, None
            
            best_time = times_seconds[0]
            top_10 = times_seconds[:10]
            average_top_10 = sum(top_10) / len(top_10)
            tenth_best = times_seconds[9] if len(times_seconds) >= 10 else None
            
            return best_time, average_top_10, tenth_best
                
        except Exception as e:
            print(f"Error scraping segment {segment_id}: {e}")
            return None, None, None
    
    async def extract_segment_data_async(self, segment_id, segment_name=None):
        """Extract data: AgentQL first, then Strava API only if leaderboard exists"""
        
        # Step 1: Check leaderboard FIRST (AgentQL)
        best_time, average_top_10, tenth_best = await self.scrape_leaderboard_times(segment_id)
        
        # If no leaderboard, save to blacklist and skip
        if best_time is None:
            print(f"  ✗ No leaderboard - adding to skip list")
            self._save_no_leaderboard_id(segment_id, segment_name)
            return None
        
        print(f"  ✓ Leaderboard found (best: {best_time}s)")
        
        # Step 2: Only now call Strava API (since we know segment is valid)
        time.sleep(0.2)  # Rate limiting
        details = self.get_segment_details(segment_id)
        if not details:
            return None
        
        time.sleep(0.2)
        streams = self.get_segment_streams(segment_id)
        
        return {
            "id": segment_id,
            "name": details.get("name"),
            "activity_type": details.get("activity_type"),
            "distance": details.get("distance"),
            "elevation_gain": details.get("total_elevation_gain"),
            "elevation_low": details.get("elevation_low"),
            "elevation_high": details.get("elevation_high"),
            "best_time": best_time,
            "average_top_10_time": round(average_top_10, 2) if average_top_10 else None,
            "tenth_best_time": tenth_best,
            "total_effort_count": details.get("effort_count"),
            "total_athlete_count": details.get("athlete_count"),
            "altitude_profile": streams.get("altitude", {}).get("data", []) if streams else [],
            "distance_profile": streams.get("distance", {}).get("data", []) if streams else [],
            "coordinates": streams.get("latlng", {}).get("data", []) if streams else []
        }
    
    def search_reunion_segments(self, max_segments=100):
        lat_min, lat_max = -21.4, -20.8
        lng_min, lng_max = 55.2, 55.8
        
        all_segments = []
        segment_ids = set()
        grid_size = 4
        lat_step = (lat_max - lat_min) / grid_size
        lng_step = (lng_max - lng_min) / grid_size
        
        for activity_type in ["riding", "running"]:
            print(f"Searching for {activity_type} segments...")
            for i in range(grid_size):
                for j in range(grid_size):
                    if len(segment_ids) >= max_segments:
                        break
                    
                    bounds = [
                        lat_min + i * lat_step,
                        lng_min + j * lng_step,
                        lat_min + (i + 1) * lat_step,
                        lng_min + (j + 1) * lng_step
                    ]
                    
                    segments = self.explore_segments(bounds, activity_type=activity_type)
                    for seg in segments:
                        if seg["id"] not in segment_ids and len(segment_ids) < max_segments:
                            segment_ids.add(seg["id"])
                            all_segments.append(seg)
                    
                    time.sleep(0.3)
        
        return all_segments[:max_segments]
    
    def load_existing_data(self):
        parquet_path = self.raw_folder / "reunion_segments.parquet"
        csv_path = self.raw_folder / "reunion_segments.csv"
        
        if parquet_path.exists():
            try:
                import pandas as pd
                df = pd.read_parquet(parquet_path)
                return df.to_dict('records'), set(df['id'].tolist())
            except:
                pass
        
        if csv_path.exists():
            existing_data = []
            existing_ids = set()
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row['id'] = int(row['id'])
                    existing_data.append(row)
                    existing_ids.add(row['id'])
            return existing_data, existing_ids
        
        return [], set()
    
    def save_data(self, data):
        if not data:
            print("No new data to save")
            return
        
        existing_data, existing_ids = self.load_existing_data()
        
        new_count = 0
        for segment in data:
            if segment['id'] not in existing_ids:
                existing_data.append(segment)
                existing_ids.add(segment['id'])
                new_count += 1
        
        print(f"Added {new_count} new segments. Total: {len(existing_data)}")
        
        # Save to Parquet (main storage - includes profiles)
        try:
            import pandas as pd
            df = pd.DataFrame(existing_data)
            parquet_path = self.raw_folder / "reunion_segments.parquet"
            df.to_parquet(parquet_path, index=False)
            print(f"Parquet saved to {parquet_path}")
        except ImportError:
            print("pandas/pyarrow not installed")
            return
        
        # Save to CSV (quick view - no profiles)
        csv_path = self.raw_folder / "reunion_segments.csv"
        fieldnames = [
            'id', 'name', 'activity_type', 'distance', 'elevation_gain',
            'elevation_low', 'elevation_high', 'best_time', 
            'average_top_10_time', 'tenth_best_time', 'total_effort_count', 
            'total_athlete_count'
        ]
        
        with open(csv_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for segment in existing_data:
                row = {k: segment.get(k) for k in fieldnames}
                writer.writerow(row)
        
        print(f"CSV saved to {csv_path}")


    def number_of_processed_segments(self):
        """
        Count how many segments are already processed by checking:
        - reunion_segments.csv
        - segments_no_leaderboard.json

        Returns:
            total_count (int)
            {
                'csv_count': X,
                'json_count': Y,
                'csv_ids': set([...]),
                'json_ids': set([...])
            }
        """
        csv_path = self.raw_folder / "reunion_segments.csv"
        json_path = self.raw_folder / "segments_no_leaderboard.json"

        csv_ids = set()
        json_ids = set()

        # --- CSV IDs ---
        if csv_path.exists():
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if "id" in row:
                            try:
                                csv_ids.add(int(row["id"]))
                            except:
                                pass
            except Exception as e:
                print(f"Error reading CSV: {e}")

        # --- JSON IDs ---
        if json_path.exists():
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                    # Your format:
                    # { "segment_ids": [id1, id2, ...] }
                    if "segment_ids" in data:
                        for sid in data["segment_ids"]:
                            try:
                                json_ids.add(int(sid))
                            except:
                                pass

            except Exception as e:
                print(f"Error reading JSON: {e}")

        # Merge unique
        total_ids = csv_ids.union(json_ids)

        return len(total_ids)

 
    async def extract_all_data_async(self, max_segments=100):
        print(f"Searching for up to {max_segments} segments...")
        segments = self.search_reunion_segments(max_segments)
        
        _, existing_ids = self.load_existing_data()
        
        # Filter: not already saved AND not in no-leaderboard list
        new_segments = [
            s for s in segments 
            if s["id"] not in existing_ids 
            and s["id"] not in self.no_leaderboard_ids
        ]
        
        skipped_no_lb = len([s for s in segments if s["id"] in self.no_leaderboard_ids])
        skipped_existing = len([s for s in segments if s["id"] in existing_ids])
        
        print(f"Total found: {len(segments)}")
        print(f"  - Already saved: {skipped_existing}")
        print(f"  - Known no leaderboard: {skipped_no_lb}")
        print(f"  - To process: {len(new_segments)}")
        
        if not new_segments:
            print("No new segments to process!")
            return []
        
        await self.init_browser()
        
        detailed_data = []
        rate_limited = False
        
        try:
            for i, seg in enumerate(new_segments, 1):
                print(f"Processing {i}/{len(new_segments)}: {seg.get('name')}")
                try:
                    data = await self.extract_segment_data_async(seg["id"], seg.get("name"))
                    if data:
                        detailed_data.append(data)
                except RateLimitException as e:
                    print(f"\n⚠️  {e}")
                    print(f"Saving {len(detailed_data)} segments collected so far...")
                    rate_limited = True
                    break
        finally:
            await self.close_browser()
        
        if rate_limited:
            print("\n💡 Tip: Wait 15 minutes and run again to continue.")
        
        return detailed_data

    
async def main():
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "config.yaml"

    ACCESS_TOKEN = get_valid_token(config_path)
    if not ACCESS_TOKEN:
        print("Failed to get valid token. Exiting.")
        return
    
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    os.environ["AGENTQL_API_KEY"] = config["agentql"]["api_key"]
    
    extractor = StravaSegmentExtractor(ACCESS_TOKEN)
    nb_existing = extractor.number_of_processed_segments()
    print(f"Already processed segments: {nb_existing}")
    data = await extractor.extract_all_data_async(max_segments=nb_existing + 50)
    extractor.save_data(data)
    
    print(f"\n{'='*50}")
    print(f"New segments extracted: {len(data)}")
    print(f"Segments without leaderboard: {len(extractor.no_leaderboard_ids)}")


if __name__ == "__main__":
    asyncio.run(main())