from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    context = browser.new_context()
    page = context.new_page()
    page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    
    page.goto("https://qasmart.superfinanciera.gov.co/login")
    print("👉 Inicia sesión manualmente en el navegador y resuelve el captcha si aparece...")
    
    # Espera a que el usuario llegue al dashboard
    page.wait_for_url("**/dashboard**", timeout=120000)
    
    # Guarda las cookies y estado de sesión
    context.storage_state(path="state.json")
    print("✅ Sesión guardada en 'state.json'")
    browser.close()