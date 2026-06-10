#!/usr/bin/env python3
"""
Selenium Configuration Helper
Provides optimized Chrome options for web scraping
"""

from selenium.webdriver.chrome.options import Options

class SeleniumConfig:
    @staticmethod
    def get_chrome_options(headless=True):
        """Get optimized Chrome options for web scraping"""
        chrome_options = Options()
        
        if headless:
            chrome_options.add_argument('--headless=new')
        
        # Performance optimizations
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-gpu-sandbox')
        chrome_options.add_argument('--disable-software-rasterizer')
        chrome_options.add_argument('--disable-webgl')
        chrome_options.add_argument('--disable-webgl2')
        chrome_options.add_argument('--disable-3d-apis')
        chrome_options.add_argument('--enable-unsafe-swiftshader')
        chrome_options.add_argument('--disable-images')  # Don't load images
        chrome_options.add_argument('--disable-javascript-harmony-shipping')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-plugins')
        chrome_options.add_argument('--disable-plugins-discovery')
        chrome_options.add_argument('--disable-preconnect')
        chrome_options.add_argument('--disable-sync')
        chrome_options.add_argument('--disable-background-timer-throttling')
        chrome_options.add_argument('--disable-renderer-backgrounding')
        chrome_options.add_argument('--disable-backgrounding-occluded-windows')
        chrome_options.add_argument('--disable-client-side-phishing-detection')
        chrome_options.add_argument('--disable-default-apps')
        chrome_options.add_argument('--disable-hang-monitor')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--disable-prompt-on-repost')
        chrome_options.add_argument('--disable-web-security')
        chrome_options.add_argument('--disable-features=TranslateUI,VizDisplayCompositor')
        chrome_options.add_argument('--window-size=1280,720')  # Smaller window
        
        # Disable logging and error messages
        chrome_options.add_argument('--log-level=3')
        chrome_options.add_argument('--silent')
        chrome_options.add_argument('--disable-logging')
        chrome_options.add_argument('--disable-gpu-logging')
        chrome_options.add_argument('--disable-extensions-http-throttling')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        # Set page load strategy to eager (don't wait for all resources)
        chrome_options.page_load_strategy = 'eager'
        
        # User agent
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
        
        return chrome_options
    
    @staticmethod
    def setup_driver_timeouts(driver):
        """Configure standard timeouts and settings for the driver"""
        # Reduce implicit wait time
        driver.implicitly_wait(5)
        
        # Set timeouts
        driver.set_page_load_timeout(15)  # Shorter timeout
        driver.set_script_timeout(10)
        
        # Execute script to remove automation detection
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        
        return driver