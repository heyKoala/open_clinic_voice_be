import requests

base_url = "http://localhost:8001/api/v1"
session = requests.Session()

def test_signup():
    print("Testing signup...")
    res = session.post(f"{base_url}/auth/signup/", json={
        "clinic_name": "Test Clinic",
        "full_name": "Test User",
        "email": "test@test.com",
        "password": "Password123!",
        "is_also_doctor": False
    })
    print(res.status_code, res.text)
    return res

def test_login():
    print("Testing login...")
    res = session.post(f"{base_url}/auth/login/", json={
        "email": "test@test.com",
        "password": "Password123!"
    })
    print(res.status_code, res.text)
    return res

def test_me():
    print("Testing me...")
    res = session.get(f"{base_url}/auth/me/")
    print(res.status_code, res.text)
    return res

def test_clinics():
    print("Testing my-clinics...")
    res = session.get(f"{base_url}/auth/my-clinics/")
    print(res.status_code, res.text)
    return res

if __name__ == "__main__":
    test_signup()
    test_login()
    test_me()
    test_clinics()
