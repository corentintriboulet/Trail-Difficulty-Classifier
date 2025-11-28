from pathlib import Path
import requests
import time
import yaml
import os
import asyncio
import agentql
import pandas as pd
from playwright.async_api import async_playwright
from Strava_Token_Manager import StravaTokenManager, make_strava_request_with_retry, RateLimitException
from Leaderboard_Extractor import LeaderboardExtractor  # ← Import the new class




def load_config(config_path):
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def setup_agentql(api_key):
    agentql.configure(api_key=api_key)
    print("✓ AgentQL configured")

class StravaSegmentExtractor:
    def __init__(self, token_manager, agentql_api_key):
        self.token_manager = token_manager
        self.agentql_api_key = agentql_api_key
        self.base_url = "https://www.strava.com/api/v3"
        self.project_root = Path(__file__).resolve().parents[2]
        self.raw_folder = self.project_root / "data" / "raw"
        os.makedirs(self.raw_folder, exist_ok=True)
        
        self.browser = None
        self.playwright = None
        self.leaderboard_extractor = None  # ← Will be initialized with browser
    
    def _make_api_request(self, url, params=None):
        """Centralized API request handler with auto-retry"""
        return make_strava_request_with_retry(self.token_manager, url, params)
    
    def explore_segments(self, bounds, activity_type="riding"):
        """Get segments in a geographic area with auto account switching"""
        url = f"{self.base_url}/segments/explore"
        params = {
            "bounds": ",".join(map(str, bounds)),
            "activity_type": activity_type
        }
        
        response = self._make_api_request(url, params)
        
        if response and response.status_code == 200:
            return response.json().get("segments", [])
        return []
    
    def get_segment_streams(self, segment_id):
        """Get altitude/distance profile with auto account switching"""
        url = f"{self.base_url}/segments/{segment_id}/streams"
        params = {"keys": "altitude,distance,latlng", "key_by_type": True}
        
        response = self._make_api_request(url, params)
        
        if response and response.status_code == 200:
            return response.json()
        # The exception is now raised directly by Strava_Token_Manager.py
        return None
    
    def get_segment_details(self, segment_id):
        """Get detailed segment info with auto account switching"""
        url = f"{self.base_url}/segments/{segment_id}"
        
        response = self._make_api_request(url)
        
        if response and response.status_code == 200:
            return response.json()
        # The exception is now raised directly by Strava_Token_Manager.py
        return None
    
    async def init_browser(self):
        """Initialize Playwright browser and LeaderboardExtractor"""
        if not self.browser:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(headless=True)
            
            # ← Initialize LeaderboardExtractor with browser
            self.leaderboard_extractor = LeaderboardExtractor(
                method="agentql",
                browser=self.browser
            )
    
    async def close_browser(self):
        """Close Playwright browser"""
        if self.browser:
            await self.browser.close()
            await self.playwright.stop()
            self.browser = None
            self.playwright = None
            self.leaderboard_extractor = None
    
    async def extract_segment_data_async(self, segment_basic_data):                                                             
        """Extract full segment data: scrape leaderboard + get details + streams"""
        segment_id = segment_basic_data["id"]
        segment_name = segment_basic_data.get("name", "Unknown")
        
        # Step 1: Scrape leaderboard using LeaderboardExtractor
        best_time, average_top_10, tenth_best = await self.leaderboard_extractor.get_times(segment_id)
        
        if best_time is None:
            print(f"  ✗ No leaderboard data")
            return None
        
        print(f"  ✓ Leaderboard: best={best_time}s, avg={average_top_10:.1f}s")
        
        # Step 2: Get segment details (API call)
        time.sleep(1.0)
        details = self.get_segment_details(segment_id)
        
        if not details:
            print(f"  ✗ Failed to get segment details")
            return None
        
        # Step 3: Get altitude profile (API call)
        time.sleep(0.2)
        streams = self.get_segment_streams(segment_id)
        
        # Combine all data
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
        """Search for segments in Reunion Island area"""
        lat_min, lat_max = -21.3980463, -20.8369424
        lng_min, lng_max = 55.1707468, 55.9040842

        #La bretagne
        #lat_min, lat_max = -21.3980463, -20.8369424
        #lng_min, lng_max = 55.4785652, 55.5090946

        all_segments = []
        segment_ids = set()
        grid_size = 7
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
        """Load existing segments from parquet or CSV"""
        parquet_path = self.raw_folder / "reunion_segments.parquet"
        csv_path = self.raw_folder / "reunion_segments.csv"
        
        if parquet_path.exists():
            try:
                df = pd.read_parquet(parquet_path)
                return df.to_dict('records'), set(df['id'].tolist())
            except Exception as e:
                print(f"Error loading parquet: {e}")
        
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path)
                return df.to_dict('records'), set(df['id'].tolist())
            except Exception as e:
                print(f"Error loading CSV: {e}")
        
        return [], set()
    
    def save_data(self, new_data):
        """Save data to parquet and CSV"""
        if not new_data:
            print("No new data to save")
            return
        
        existing_data, existing_ids = self.load_existing_data()
        
        added_count = 0
        for segment in new_data:
            if segment['id'] not in existing_ids:
                existing_data.append(segment)
                existing_ids.add(segment['id'])
                added_count += 1
        
        if added_count == 0:
            print("No new segments added (all were duplicates)")
            return
        
        print(f"Added {added_count} new segments. Total: {len(existing_data)}")
        
        df = pd.DataFrame(existing_data)
        
        # Save Parquet
        parquet_path = self.raw_folder / "reunion_segments.parquet"
        df.to_parquet(parquet_path, index=False)
        print(f"✓ Parquet saved: {parquet_path}")
        
        # Save CSV
        csv_path = self.raw_folder / "reunion_segments.csv"
        summary_cols = [
            'id', 'name', 'activity_type', 'distance', 'elevation_gain',
            'elevation_low', 'elevation_high', 'best_time', 
            'average_top_10_time', 'tenth_best_time', 'total_effort_count', 
            'total_athlete_count'
        ]
        
        cols_to_save = [c for c in summary_cols if c in df.columns]
        df[cols_to_save].to_csv(csv_path, index=False, na_rep='')
        print(f"✓ CSV saved: {csv_path}")
    
    def number_of_processed_segments(self):
        """Count total processed segments"""
        _, existing_ids = self.load_existing_data()
        return len(existing_ids)
    
    async def extract_all_data_async(self, max_segments=100):
        """Main extraction pipeline"""
        print(f"Searching for up to {max_segments} segments...")
        
        all_segments = self.search_reunion_segments(max_segments)
        
        _, existing_ids = self.load_existing_data()
        new_segments = [s for s in all_segments if s["id"] not in existing_ids]
        
        print(f"\nTotal found: {len(all_segments)}")
        print(f"  Already saved: {len(all_segments) - len(new_segments)}")
        print(f"  To process: {len(new_segments)}")
        
        if not new_segments:
            print("No new segments to process!")
            return []
        
        await self.init_browser()
        detailed_data = []
        
        try:
            for i, seg in enumerate(new_segments, 1):
                print(f"\nProcessing {i}/{len(new_segments)}: {seg.get('name')}")
                try:
                    data = await self.extract_segment_data_async(seg)
                    if data:
                        detailed_data.append(data)
                except RateLimitException as e:
                    print(f"\n⚠️ {e}")
                    print(f"Saving {len(detailed_data)} segments collected so far...")
                    break
        finally:
            await self.close_browser()
        
        return detailed_data


async def main():
    """Main execution with multi-account support and clean exit handling"""
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "config.yaml"
    
    token_manager = StravaTokenManager(config_path)
    print(f"✓ Using account: {token_manager.get_current_account()['name']}")
    
    config = token_manager.config
    AGENTQL_API_KEY = config["agentql"]["api_key"]
    setup_agentql(AGENTQL_API_KEY)
    
    extractor = StravaSegmentExtractor(token_manager, AGENTQL_API_KEY)
    
    nb_existing = extractor.number_of_processed_segments()
    print(f"Already processed segments: {nb_existing}")
    
    target_segments = nb_existing + 50
    
    # Initialize data to an empty list in case the exception happens immediately
    data = [] 
    
    try:
        # ⚠️ Wrap the main extraction call to catch the RateLimitException
        data = await extractor.extract_all_data_async(max_segments=target_segments)
        
    except RateLimitException as e:
        # Catch the intended stop signal and print a clean message
        print(f"\n❌ Script terminated cleanly by rate limit: {e}")
        # 'data' will contain segments collected inside the loop, or [] if error occurred during search.
        pass
        
    # The 'data' variable now holds all collected segments, even if the script was interrupted.
    extractor.save_data(data)
    
    print(f"\n{'='*50}")
    print(f"EXTRACTION SUMMARY")
    print(f"{'='*50}")
    print(f"New segments extracted: {len(data)}")
    print(f"Total processed: {extractor.number_of_processed_segments()}")
    token_manager.print_status()
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())