import requests

symbol = "SPY"
day = "2026-02-17"  # last trading day
start = f"{day}T14:30:00Z"
end   = f"{day}T14:35:00Z"

url = (
    "https://data.alpaca.markets/v2/stocks/bars"
    f"?symbols={symbol}"
    "&timeframe=5Min"
    f"&start={start}"
    f"&end={end}"
    "&limit=1"
    "&adjustment=raw"
    "&feed=sip"
    "&sort=asc"
)

headers = {
    "accept": "application/json",
    "APCA-API-KEY-ID": "PK5VAWB3KOPSAWAAUIAD7V6TVQ",
    "APCA-API-SECRET-KEY": "34Dqf7CnodY6Le5FBkNdd59fxpTus4hkyRGqgeY5PiYi"
}

response = requests.get(url, headers=headers)
print(response.json())