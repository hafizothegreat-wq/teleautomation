import undetected_chromedriver as uc

options = uc.ChromeOptions()

driver = uc.Chrome(
    version_main=147,
    options=options,
    headless=True
)

driver.get("https://example.com")

print(driver.title)

driver.quit()