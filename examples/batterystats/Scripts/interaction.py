import time
def main(device, *args, **kwargs):
    # print('=INTERACTION=')
    package_name = 'com.android.chrome'
    main_activity = 'com.google.android.apps.chrome.Main'
    url = "https://greenlab.myddns.me/C-Based/C/02_nbody/dist/wasm_exec.html"
    print("Visiting C/nbody")
    device.launch_activity(package_name, main_activity, data_uri=url,action='android.intent.action.VIEW')
    print("Visited C/nBody")
