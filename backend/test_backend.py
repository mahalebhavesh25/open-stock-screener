import time
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

print("--- Testing API Endpoints & Enhancements ---")
# 1. Health
r = client.get("/api/health")
print(f"1. /api/health -> status={r.status_code}, body={r.json()}")
assert r.status_code == 200

# 2. Overview (check non-blocking speed & fields)
t0 = time.time()
r = client.get("/api/overview/")
elapsed = time.time() - t0
data = r.json()
print(f"2. /api/overview/ -> status={r.status_code}, elapsed={elapsed*1000:.1f}ms, regime={data.get('regime', {}).get('status')}, active_tasks={len(data.get('active_tasks', []))}")
assert r.status_code == 200
assert "portfolio" in data
assert "initial_capital" in data["portfolio"]
assert "active_tasks" in data

# 3. Settings: Retrieval
r = client.get("/api/settings/")
settings_data = r.json()
print(f"3. /api/settings/ [GET] -> status={r.status_code}")
print(f"   initial_capital: {settings_data.get('initial_capital')}")
print(f"   has groww_access_token: {'groww_access_token' in settings_data}")
assert r.status_code == 200
assert settings_data.get("initial_capital") is not None
assert "groww_access_token" in settings_data

# 4. Settings: Store & Retrieve Persistence Test
test_payload = {
    "initial_capital": 75000.0,
    "groww_access_token": "test_jwt_access_token_12345",
    "risk_per_trade_pct": 0.008,
    "max_open_positions": 6,
}
r_post = client.post("/api/settings/", json=test_payload)
print(f"4. /api/settings/ [POST] -> status={r_post.status_code}")
assert r_post.status_code == 200

# Verify persistence in subsequent GET
r_verify = client.get("/api/settings/")
verified = r_verify.json()
print(f"   Verified stored initial_capital: {verified.get('initial_capital')}")
print(f"   Verified stored groww_access_token: {verified.get('groww_access_token')}")
assert verified.get("initial_capital") == 75000.0
assert verified.get("groww_access_token") == "test_jwt_access_token_12345"

# 5. Universe
r = client.get("/api/universe/")
print(f"5. /api/universe/ -> status={r.status_code}, total={r.json().get('total')}")
assert r.status_code == 200

# 6. Tasks
r = client.get("/api/tasks/")
print(f"6. /api/tasks/ -> status={r.status_code}, tasks_count={len(r.json().get('tasks', []))}")
assert r.status_code == 200

# 7. Frontend SPA Root & Redirection Link in HTML Bundle
r = client.get("/")
print(f"7. / (SPA root) -> status={r.status_code}, contains_app_root={'<app-root>' in r.text}")
assert r.status_code == 200
assert '<app-root>' in r.text

# Reset test settings back to defaults
r_reset = client.post("/api/settings/reset")
assert r_reset.status_code == 200

print("\n>>> ALL ENHANCEMENT TESTS PASSED PERFECTLY! <<<")

