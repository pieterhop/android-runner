from .Browsers import Chrome, Firefox, Opera, Samsung


class BrowserFactory(object):
    @staticmethod
    def get_browser(name):
        if name == "chrome":
            return Chrome.Chrome
        if name == "firefox":
            return Firefox.Firefox
        if name == "opera":
            return Opera.Opera
        if name == "samsung":
            return Samsung.Samsung
        raise Exception("No Browser found")
