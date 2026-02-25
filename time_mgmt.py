from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time

class TimeMgr:
    def __init__(self):

        self.eastern = ZoneInfo("America/New_York")

        self.current_dt = datetime.now(self.eastern)

        self.next_day_930 = (self.current_dt + timedelta(days=1)).replace(
            hour=9, minute=39, second=0, microsecond=0
        )

        self.today_930 = self.current_dt.replace(
                hour=9, minute=30, second=0, microsecond=0
        )

        self.next_day_935 = (self.current_dt + timedelta(days=1)).replace(
            hour=9, minute=35, second=0, microsecond=0
        )

        self.today_935 = self.current_dt.replace(
                hour=9, minute=35, second=0, microsecond=0
        )

        self.today_1630 = self.current_dt.replace(
            hour=16, minute=30, second=0, microsecond=0
        )



    def wait_until_next_minute(self):
        now = datetime.now(self.eastern)
        next_minute = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
        delay = (next_minute - now).total_seconds()
        if delay > 0:
            time.sleep(delay)


    def wait_until(self, target_datetime):
        now = datetime.now(target_datetime.tzinfo)
        diff = (target_datetime - now).total_seconds()
        
        if diff > 0:
            time.sleep(diff)
        #print(f"Waited until {datetime.now()}")

    def market_still_open(self):
        now = datetime.now(self.eastern)
        return (self.today_1630 - now).total_seconds() > 60


