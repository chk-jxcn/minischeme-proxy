import pytest
from proxy.database import Database, mask_sensitive_value


def test_mask_sensitive_value():
    assert mask_sensitive_value("api_token", "sk-1234567890abcdef") == "sk****ef"
    assert mask_sensitive_value("weather_key", "mysecretkey999") == "my****99"
    assert mask_sensitive_value("city_name", "Beijing") == "Beijing"
    assert mask_sensitive_value("pwd", "123") == "***"


def test_database_crud():
    db = Database(":memory:")
    mac = "C8F09EB36C90"
    hw_hash = b"hw_hash_bytes_32_abcdefghijklmn"
    m2 = b"master_2_bytes_32_abcdefghijklmn"

    # Register
    db.register_device(mac, hw_hash, m2, "192.168.1.100")
    dev = db.get_device(mac)
    assert dev is not None
    assert dev["mac"] == mac
    assert dev["hardware_hash_key"] == hw_hash

    # App Data
    db.set_app_data(mac, "weather", "city", "Shanghai")
    db.set_app_data(mac, "weather", "api_token", "secret_token_123456")

    items = db.get_app_data(mac, "weather", masked=True)
    assert len(items) == 2
    for it in items:
        if it["key"] == "api_token":
            assert "****" in it["value"]
        if it["key"] == "city":
            assert it["value"] == "Shanghai"

    # Logs
    db.log_access(mac, "192.168.1.100", "GET", "/app/weather", 200, 150)
    logs = db.get_device_logs(mac)
    assert len(logs) == 1
    assert logs[0]["endpoint"] == "/app/weather"

    # Admin list
    devs = db.admin_list_devices()
    assert len(devs) == 1

    # Delete account
    db.delete_device_account(mac)
    assert db.get_device(mac) is None
    assert len(db.get_app_data(mac)) == 0
    assert len(db.get_device_logs(mac)) == 0


def test_database_file_persistence(tmp_path):
    """Verifies that SQLite database is correctly persisted as a disk file."""
    db_file = str(tmp_path / "sub_dir" / "test_persist.db")
    db1 = Database(db_file)
    mac = "AABBCCDDEEFF"
    hw_hash = b"hw_hash_test_1234567890abcdef12"
    m2 = b"master_2_test_1234567890abcdef12"

    db1.register_device(mac, hw_hash, m2, "192.168.1.50")
    db1.set_app_data(mac, "stocks", "codes", "sh600519")
    db1.close()

    # Re-open database from disk file and verify data survived process termination
    db2 = Database(db_file)
    dev = db2.get_device(mac)
    assert dev is not None
    assert dev["mac"] == mac
    assert dev["hardware_hash_key"] == hw_hash
    assert dev["master_key_2"] == m2

    data = db2.get_app_data(mac, "stocks")
    assert len(data) == 1
    assert data[0]["key"] == "codes"
    assert data[0]["value"] == "sh600519"
    db2.close()
