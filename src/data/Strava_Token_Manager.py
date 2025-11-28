from pathlib import Path
import requests
import yaml
import time
from datetime import datetime, timedelta
from typing import Optional, Dict

class RateLimitException(Exception):
    """Raised when Strava API rate limit is hit"""
    pass

class StravaTokenManager:
    """Manages multiple Strava accounts with fixed 15-min rate limit windows"""
    
    # Strava limits: 100 calls per 15min, 1000 calls per day
    CALLS_PER_15MIN = 100
    CALLS_PER_DAY = 1000
    WINDOW_MINUTES = 15
    
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = self._load_config()
        self.accounts = self._load_accounts()
        self.current_account_idx = 0
        
        # Track usage per account
        self.account_stats = {}
        for account in self.accounts:
            name = account['name']
            self.account_stats[name] = {
                'calls_this_window': 0,
                'calls_today': 0,
                'current_window_start': self._get_current_window_start(),
                'day_start': datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            }
    
    def _get_current_window_start(self) -> datetime:
        """Get the start time of current 15-min window (e.g., 20:00, 20:15, 20:30)"""
        now = datetime.now()
        minutes_into_hour = now.minute
        window_number = minutes_into_hour // self.WINDOW_MINUTES
        window_start_minute = window_number * self.WINDOW_MINUTES
        
        return now.replace(minute=window_start_minute, second=0, microsecond=0)
    
    def _get_next_window_start(self) -> datetime:
        """Get when the next 15-min window starts"""
        current = self._get_current_window_start()
        return current + timedelta(minutes=self.WINDOW_MINUTES)
    
    def _reset_window_if_needed(self, account_name: str):
        """Reset call counter if we're in a new 15-min window"""
        stats = self.account_stats[account_name]
        current_window = self._get_current_window_start()
        
        # Check if window changed
        if current_window > stats['current_window_start']:
            old_calls = stats['calls_this_window']
            stats['calls_this_window'] = 0
            stats['current_window_start'] = current_window
            print(f"🔄 {account_name}: New window started (previous: {old_calls}/100 calls)")
        
        # Check if day changed
        current_day = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        if current_day > stats['day_start']:
            stats['calls_today'] = 0
            stats['day_start'] = current_day
            print(f"📅 {account_name}: New day started - daily limit reset")
    
    def update_usage_from_headers(self, account_name: str, headers: dict):
        """
        Critical Fix: Sync local counters with Strava's server-side counters
        Reads 'X-RateLimit-Usage' header: "window_usage,daily_usage"
        """
        usage = headers.get('X-RateLimit-Usage')
        if usage:
            try:
                parts = usage.split(',')
                if len(parts) >= 2:
                    window_used = int(parts[0])
                    day_used = int(parts[1])
                    
                    stats = self.account_stats[account_name]
                    
                    # Update if server reports higher usage (prevents issues after script restart)
                    if window_used > stats['calls_this_window']:
                        stats['calls_this_window'] = window_used
                    if day_used > stats['calls_today']:
                        stats['calls_today'] = day_used
                        
            except (ValueError, IndexError):
                pass

    def _load_config(self) -> dict:
        with open(self.config_path, 'r') as f:
            return yaml.safe_load(f)
    
    def _load_accounts(self) -> list:
        accounts = []
        if 'strava' in self.config:
            accounts.append({'name': 'strava', 'config': self.config['strava']})
        for key in sorted(self.config.keys()):
            if key.startswith('strava') and key != 'strava':
                accounts.append({'name': key, 'config': self.config[key]})
        print(f"✓ Loaded {len(accounts)} Strava account(s): {[a['name'] for a in accounts]}")
        return accounts
    
    def _save_config(self):
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config, f, default_flow_style=False)
    
    def _refresh_token(self, account_name: str) -> Optional[str]:
        account = next(a for a in self.accounts if a['name'] == account_name)
        strava_config = account['config']
        
        print(f"🔄 Refreshing token for {account_name}...")
        try:
            response = requests.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id": strava_config['client_id'],
                    "client_secret": strava_config['client_secret'],
                    "grant_type": "refresh_token",
                    "refresh_token": strava_config['refresh_token']
                },
                timeout=10
            )
            
            if response.status_code == 200:
                tokens = response.json()
                self.config[account_name]['access_token'] = tokens['access_token']
                self.config[account_name]['refresh_token'] = tokens['refresh_token']
                self._save_config()
                account['config']['access_token'] = tokens['access_token']
                account['config']['refresh_token'] = tokens['refresh_token']
                print(f"✓ Token refreshed for {account_name}")
                return tokens['access_token']
            else:
                print(f"✗ Token refresh failed for {account_name}: {response.text}")
                return None
        except Exception as e:
            print(f"✗ Network error during refresh: {e}")
            return None
    
    def _validate_token(self, access_token: str) -> bool:
        try:
            response = requests.get(
                "https://www.strava.com/api/v3/athlete",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10
            )
            return response.status_code == 200
        except:
            return False
    
    def get_current_account(self) -> Dict:
        return self.accounts[self.current_account_idx]
    
    def can_make_call(self, account_name: str) -> bool:
        self._reset_window_if_needed(account_name)
        stats = self.account_stats[account_name]
        return (stats['calls_this_window'] < self.CALLS_PER_15MIN and 
                stats['calls_today'] < self.CALLS_PER_DAY)
    
    def record_api_call(self, account_name: str):
        self._reset_window_if_needed(account_name)
        stats = self.account_stats[account_name]
        stats['calls_this_window'] += 1
        stats['calls_today'] += 1
    
    def get_valid_token(self) -> str:
        account = self.get_current_account()
        account_name = account['name']
        access_token = account['config']['access_token']
        return access_token
    
    def switch_account(self) -> bool:
        for offset in range(1, len(self.accounts)):
            next_idx = (self.current_account_idx + offset) % len(self.accounts)
            account_name = self.accounts[next_idx]['name']
            if self.can_make_call(account_name):
                self.current_account_idx = next_idx
                stats = self.account_stats[account_name]
                print(f"🔄 Switched to {account_name} (Status: {stats['calls_this_window']}/100 window)")
                return True
        return False
    
    def get_headers(self) -> dict:
        token = self.get_valid_token()
        return {"Authorization": f"Bearer {token}"}
    
    def handle_rate_limit(self) -> bool:
        """
        Handles rate limits by switching accounts.
        CRITICAL CHANGE: Raises RateLimitException if ALL accounts are exhausted.
        """
        current_account = self.get_current_account()['name']
        print("\n" + "="*60)
        print(f"⚠️  RATE LIMIT HIT for {current_account}")
        
        # Max out the stats so we don't try this account again immediately
        self.account_stats[current_account]['calls_this_window'] = self.CALLS_PER_15MIN
        
        if self.switch_account():
            print("✓ Continuing with new account")
            print("="*60 + "\n")
            return True
        
        # --- STOPPING LOGIC ---
        print("⚠️  All accounts exhausted. Stopping script.")
        print(f"⏰ Next window starts at: {self._get_next_window_start().strftime('%H:%M:%S')}")
        print("="*60 + "\n")
        
        # Raise the exception directly to force immediate exit of the call stack
        raise RateLimitException("All Strava accounts are rate-limited, forcing stop.")
    
    def print_status(self):
        print(f"\n📊 API Usage Status (Window: {self._get_current_window_start().strftime('%H:%M')})")
        print("-" * 60)
        for account in self.accounts:
            name = account['name']
            stats = self.account_stats[name]
            current = "← ACTIVE" if name == self.get_current_account()['name'] else ""
            print(f"{name:12} | Window: {stats['calls_this_window']:3}/100 | "
                  f"Day: {stats['calls_today']:4}/1000 {current}")
        print("-" * 60)

def make_strava_request_with_retry(token_manager: StravaTokenManager, 
                                   url: str, 
                                   params: dict = None,
                                   max_retries: int = 5) -> Optional[requests.Response]:
    """
    Make a Strava API request with automatic account switching and header syncing.
    """
    for attempt in range(max_retries):
        current_account = token_manager.get_current_account()['name']
        
        # The two calls below will now RAISE RateLimitException and immediately exit this function
        # if all accounts are exhausted, solving the infinite loop.
        
        if not token_manager.can_make_call(current_account):
            token_manager.handle_rate_limit() # Will raise on exhaustion
            # If switch succeeded, continue
            current_account = token_manager.get_current_account()['name']
        
        headers = token_manager.get_headers()
        try:
            response = requests.get(url, headers=headers, params=params, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f"Network error: {e}, retrying...")
            time.sleep(2)
            continue
        
        # SYNC: Update local stats from headers
        token_manager.update_usage_from_headers(current_account, response.headers)
        
        if response.status_code == 200:
            token_manager.record_api_call(current_account)
            return response
        
        elif response.status_code == 429:
            token_manager.handle_rate_limit() # Will raise on exhaustion
            continue
        
        elif response.status_code == 401:
            print(f"🔄 Token expired for {current_account}, refreshing...")
            token_manager._refresh_token(current_account)
            continue
            
        else:
            print(f"❌ API Error {response.status_code}: {response.text}")
            return None
            
    print(f"❌ Max retries ({max_retries}) reached")
    return None
                                  