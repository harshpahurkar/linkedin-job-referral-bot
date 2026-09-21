import urllib.parse

from selenium import webdriver
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

from utils import OFFSCREEN_ARGS

_PAGE = '<button onclick="this.dataset.clicked = 1" style="margin: 200px">Connect</button>'


def test_offscreen_window_still_paints_and_takes_real_clicks():
    options = webdriver.ChromeOptions()
    for arg in OFFSCREEN_ARGS:
        options.add_argument(arg)
    driver = webdriver.Chrome(options=options)
    try:
        driver.set_script_timeout(5)
        driver.get("data:text/html;charset=utf-8," + urllib.parse.quote(_PAGE))
        button = driver.find_element(By.TAG_NAME, "button")

        assert driver.get_window_position()["x"] < -2000  # nowhere a monitor could be
        assert driver.execute_script("return document.visibilityState") == "visible"
        driver.execute_async_script("requestAnimationFrame(arguments[0])")  # frames still tick
        ActionChains(driver).move_to_element(button).click().perform()
        assert button.get_attribute("data-clicked") == "1"
    finally:
        driver.quit()
