# main.py
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
import time

def login_to_twitter():
    options = Options()
    options.add_argument('--headless')  # بدون واجهة رسومية
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(options=options)

    try:
        driver.get("https://twitter.com/login")
        time.sleep(5)

        username = driver.find_element(By.NAME, "text")
        username.send_keys("YOUR_USERNAME")
        driver.find_element(By.XPATH, '//span[text()="Next"]').click()
        time.sleep(3)

        password = driver.find_element(By.NAME, "password")
        password.send_keys("YOUR_PASSWORD")
        driver.find_element(By.XPATH, '//span[text()="Log in"]').click()

        time.sleep(5)
        print("✅ تم تسجيل الدخول بنجاح!")

    except Exception as e:
        print(f"❌ حدث خطأ: {e}")
    finally:
        driver.quit()

if __name__ == "__main__":
    login_to_twitter()
