import datetime

def parse_time(dt: datetime) -> datetime:
   if dt.year < 1971:
      dt = datetime(0, 1, 1, 0, 0, 0)
   dt = dt.astimezone(tz=datetime.datetime.now().astimezone().tzinfo)
   return dt
