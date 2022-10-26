import csv
import logging
import os
import os.path as op
import subprocess
import time
import shlex
import zlib
import re
from collections import OrderedDict

from AndroidRunner.Plugins.batterystats import BatterystatsParser
from AndroidRunner.BrowserFactory import BrowserFactory
from AndroidRunner.Plugins.Profiler import Profiler
from functools import reduce
from AndroidRunner import Tests
from AndroidRunner import util

class Batterystats(Profiler):

    ANDROID_VERSION_11_API_LEVEL_30 = 30
    def __init__(self, config, paths):
        super(Batterystats, self).__init__(config, paths)
        self.output_dir = ''
        self.paths = paths
        self.profile = False
        self.cleanup = config.get('cleanup')
        self.enable_systrace_parsing = config.get('enable_systrace_parsing', True)
        self.python2_path = config.get('python2_path', 'python2')

        # "config" only passes the fields under "profilers", so config.json is loaded again for the fields below
        # FIX
        config_f = util.load_json(op.join(self.paths["CONFIG_DIR"], self.paths['ORIGINAL_CONFIG_DIR']))
        self.type = config_f['type']
        self.systrace = config_f.get('systrace_path', 'systrace')
        self.powerprofile = config_f['powerprofile_path']
        self.duration = Tests.is_integer(config_f.get('duration', 0)) / 1000
        if self.type == 'web':
            self.browsers = [BrowserFactory.get_browser(b)() for b in config_f.get('browsers', ['chrome'])]

        if os.path.exists(self.systrace): # If it does not exist, then there might be a prefix already added to the path
            self.systrace  = ' '.join([self.python2_path, self.systrace])
        else:
            print("Did not prefix python2 path to systrace path due to the systrace path not existing. " + \
                  "This is fine if you added a prefix path yourself, if not, double check the systrace_path inside of your config and make sure it exists.")

    # noinspection PyGlobalUndefined
    def start_profiling(self, device, **kwargs):
        # Reset logs on the device
        device.shell('dumpsys batterystats --reset')
        print('Batterystats cleared')

        # Create output directories
        global app
        global systrace_file
        global logcat_file
        global batterystats_file
        global results_file
        global results_file_name

        if self.type == 'native':
            app = kwargs.get('app', None)
        # TODO: add support for other browsers, required form: app = 'package.name'
        elif self.type == 'web':
            app = kwargs['browser'].package_name

        # Create files on system
        systrace_file = op.join(self.output_dir,
                                'systrace_{}_{}.html'.format(device.id, time.strftime('%Y.%m.%d_%H%M%S')))
        logcat_file = op.join(self.output_dir, 'logcat_{}_{}.txt'.format(device.id, time.strftime('%Y.%m.%d_%H%M%S')))
        batterystats_file = op.join(self.output_dir, 'batterystats_history_{}_{}.txt'.format(device.id, time.strftime(
            '%Y.%m.%d_%H%M%S')))
        print(batterystats_file)
        results_file_name = 'results_{}_{}.csv'.format(device.id, time.strftime('%Y.%m.%d_%H%M%S'))
        results_file = op.join(self.output_dir, results_file_name)

        self.profile = True
        self.get_data(device, app)

    # noinspection PyGlobalUndefined
    def get_data(self, device, application):
        """Runs the systrace method for self.duration seconds in a separate thread"""
        # TODO: Check if 'systrace freq idle' is supported by the device
        global sysproc
        self._errored = False # FIXME: dirty hack

        # Run systrace in another thread.
        ps = subprocess.run(['adb', 'shell', 'mkdir -p /storage/self/primary/tmp'], check=True, stderr=subprocess.PIPE)
        if ps.stderr != b'':
            raise RuntimeError(ps.stderr)
        subprocess.run(['adb', 'shell', 'echo 0 > /sys/kernel/debug/tracing/tracing_on'], check=True) # disable tracing in case it was enabled
        time.sleep(1)
        # subprocess.run(['adb', 'shell', 'cat /sys/kernel/debug/tracing/trace > /dev/null'], check=True, stdout=subprocess.DEVNULL) # Should we clear ring buffer?
        ps = subprocess.run(shlex.split(
            f'adb shell atrace -z -b 10240 -a {application} freq idle --async_start'), # buffer size is per-cpu, so you get a lot more
            check=True, stderr=subprocess.PIPE
        )
        if ps.stderr != b'':
            # stop_profiling will take care of stopping atrace because even if it errors it does not stop
            self._errored = True
            raise RuntimeError(ps.stderr.decode('ascii'))

    def stop_profiling(self, device, **kwargs):
        # batterystats does not need to be stopped. systrace is stopped in the collect_results function
        # since it needs to run a little bit longer than the rest (see original implementation where it runs for 5 secs longer)
        ps = subprocess.run(shlex.split(
            f'adb shell atrace -z --async_stop -o /storage/self/primary/tmp/atrace.out'),
            check=True,
            stderr=subprocess.PIPE
        )
        if not ps.stderr == b'':
            raise RuntimeError(ps.stderr)
        if self._errored:
            return
        self.profile = False

        # self._systrace_content = ps.stdout.decode('ascii') #FIXME: dirty hack
        # self.logger.debug('self._systrace_content: ' + self._systrace_content)

    # Pull logcat file from device
    @staticmethod
    def pull_logcat(device):
        """
        From Android 11 (API level 30) the path /mnt/sdcard cannot be accessed via ADB
        as you don't have permissions to access this path. However, we can access /sdcard.
        """
        device_api_version = int(device.shell("getprop ro.build.version.sdk"))

        if device_api_version >= Batterystats.ANDROID_VERSION_11_API_LEVEL_30:
            logcat_output_file_device_dir_path = "/sdcard"
        else:
            logcat_output_file_device_dir_path = "/mnt/sdcard"

        device.shell(f"logcat -f {logcat_output_file_device_dir_path}/logcat.txt -d")
        device.pull(f"{logcat_output_file_device_dir_path}/logcat.txt", logcat_file)
        device.shell(f"rm -f {logcat_output_file_device_dir_path}/logcat.txt")

    # Get BatteryStats data
    def get_batterystats_results(self, device):
        with open(batterystats_file, 'w+') as f:
            f.write(device.shell('dumpsys batterystats --history'))
        batterystats_results = BatterystatsParser.parse_batterystats(app, batterystats_file, self.powerprofile)
        return batterystats_results

    # Estimate total consumption, charge is given in mAh, volt in mV
    @staticmethod
    def get_consumed_joules(device):
        charge = device.shell('dumpsys batterystats | grep "Computed drain:"').split(',')[1].split(':')[1]
        volt = device.shell('dumpsys batterystats | grep "volt="').split('volt=')[1].split()[0]
        energy_consumed_wh = float(charge) * float(volt) / 1000000.0
        energy_consumed_j = energy_consumed_wh * 3600.0
        return energy_consumed_j

    def get_systrace_results(self, device):
        logger = logging.getLogger(self.__class__.__name__)
        ps = subprocess.run(['adb', 'pull', '/storage/self/primary/tmp/atrace.out', systrace_file], check=True, stderr=subprocess.PIPE)
        if not ps.stderr == b'':
            raise RuntimeError(ps.stderr)

        def strip_and_decompress_trace(trace_data: bytes):
            """From atrace_agent.py
            Fixes new-lines and decompresses trace data.

            Args:
              trace_data: The trace data returned by atrace.
            Returns:
              The decompressed trace data.
            """
            # Collapse CRLFs that are added by adb shell.
            if trace_data.startswith(b'\r\n'):
                trace_data = trace_data.replace(b'\r\n', b'\n')
            elif trace_data.startswith(b'\r\r\n'):
                # On windows, adb adds an extra '\r' character for each line.
                trace_data = trace_data.replace(b'\r\r\n', b'\n')

            # Skip the initial newline.
            if trace_data[0:1] == b'\n':
                trace_data = trace_data[1:]

            if not trace_data.startswith(b'# tracer'):
                # No header found, so assume the data is compressed.
                trace_data = zlib.decompress(trace_data)

            # Enforce Unix line-endings.
            trace_data = trace_data.replace(b'\r', b'')

            # Skip any initial newlines.
            while trace_data and trace_data[0:1] == b'\n':
                trace_data = trace_data[1:]

            return trace_data

        def _collect_trace_data():
            """Reads the output from atrace and stops the trace."""
            TRACE_START_REGEXP = rb'TRACE\:'
            ADB_IGNORE_REGEXP = rb'^capturing trace\.\.\. done|^capturing trace\.\.\.'

            with open(systrace_file, 'rb') as f:
                result = f.read()

            data_start = re.search(TRACE_START_REGEXP, result)
            if data_start:
                data_start = data_start.end(0)
            else:
                raise IOError('Unable to get atrace data. Did you forget adb root?')
            output = re.sub(ADB_IGNORE_REGEXP, b'', result[data_start:])
            return output

        data=_collect_trace_data()
        data = strip_and_decompress_trace(data)
        with open(systrace_file, 'w') as f: # overwrite file
            f.write(data.decode('ascii'))

        cores = int(device.shell('cat /proc/cpuinfo | grep processor | wc -l'))

        systrace_results = []
        if self.enable_systrace_parsing: 
            device_api_version = int(device.shell("getprop ro.build.version.sdk"))
            systrace_results = BatterystatsParser.parse_systrace(app, systrace_file, logcat_file, batterystats_file, \
                                                                self.powerprofile, cores, device_api_version)
        return systrace_results

    def write_results(self, batterystats_results, systrace_results, energy_consumed_j):
        with open(results_file, 'w+') as results:
            writer = csv.writer(results, delimiter="\n")
            writer.writerow(
                ['Start Time (Seconds),End Time (Seconds),Duration (Seconds),Component,Energy Consumption (Joule)'])
            writer.writerow(batterystats_results)
            writer.writerow(systrace_results)
        # FIX
        with open(op.join(self.output_dir, 'Joule_{}'.format(results_file_name)), 'w+') as out:
            out.write('Joule_calculated\n{}\n'.format(energy_consumed_j))

    def cleanup_logs(self):
        if self.cleanup is True:
            # Remove log files
            os.remove(systrace_file)
            os.remove(logcat_file)
            os.remove(batterystats_file)
            subprocess.run(['adb', 'shell', 'rm -f /storage/self/primary/tmp/atrace.out'], check=True)

    def collect_results(self, device, path=None):
        self.pull_logcat(device)
        batterystats_results = self.get_batterystats_results(device)
        energy_consumed_j = self.get_consumed_joules(device)
        systrace_results = self.get_systrace_results(device)

        self.write_results(batterystats_results, systrace_results, energy_consumed_j)
        self.cleanup_logs()

    def set_output(self, output_dir):
        self.output_dir = output_dir

    def dependencies(self):
        return []

    def load(self, device):
        return

    def unload(self, device):
        return

    def aggregate_subject(self):
        filename = os.path.join(self.output_dir, 'Aggregated.csv')
        current_row = self.aggregate_battery_subject(self.output_dir, False)
        current_row.update(self.aggregate_battery_subject(self.output_dir, True))
        subject_rows = list()
        subject_rows.append(current_row)

        util.write_to_file(filename, subject_rows)

    def aggregate_end(self, data_dir, output_file):
        # FIX
        rows = self.aggregate_final(data_dir)

        util.write_to_file(output_file, rows)

    @staticmethod
    def aggregate_battery_subject(logs_dir, joules):
        def add_row(accum, new):
            row = {k: v + float(new[k]) for k, v in list(accum.items()) if k not in ['Component', 'count']}
            count = accum['count'] + 1
            return dict(row, **{'count': count})

        # FIX
        runs = []
        runs_total = dict()
        for run_file in [f for f in os.listdir(logs_dir) if os.path.isfile(os.path.join(logs_dir, f))]:
            if ('Joule' in run_file) and joules:
                with open(os.path.join(logs_dir, run_file), 'r', encoding='utf-8') as run:
                    reader = csv.DictReader(run)
                    init = dict({fn: 0 for fn in reader.fieldnames if fn != 'datetime'}, **{'count': 0})
                    run_total = reduce(add_row, reader, init)
                    runs.append({k: v / run_total['count'] for k, v in list(run_total.items()) if k != 'count'})
                runs_total = reduce(lambda x, y: {k: v + y[k] for k, v in list(x.items())}, runs)
        return OrderedDict(
            sorted(list({'batterystats_' + k: v / len(runs) for k, v in list(runs_total.items())}.items()), key=lambda x: x[0]))

    def aggregate_final(self, data_dir):
        rows = []
        for device in util.list_subdir(data_dir):
            row = OrderedDict({'device': device})
            device_dir = os.path.join(data_dir, device)
            for subject in util.list_subdir(device_dir):
                row.update({'subject': subject})
                subject_dir = os.path.join(device_dir, subject)
                if os.path.isdir(os.path.join(subject_dir, 'batterystats')):
                    row.update(self.aggregate_battery_final(os.path.join(subject_dir, 'batterystats')))
                    rows.append(row.copy())
                else:
                    for browser in util.list_subdir(subject_dir):
                        row.update({'browser': browser})
                        browser_dir = os.path.join(subject_dir, browser)
                        if os.path.isdir(os.path.join(browser_dir, 'batterystats')):
                            row.update(self.aggregate_battery_final(os.path.join(browser_dir, 'batterystats')))
                            rows.append(row.copy())
        return rows

    @staticmethod
    def aggregate_battery_final(logs_dir):
        for aggregated_file in [f for f in os.listdir(logs_dir) if os.path.isfile(os.path.join(logs_dir, f))]:
            if aggregated_file == "Aggregated.csv":
                with open(os.path.join(logs_dir, aggregated_file), 'r', encoding='utf-8') as aggregated:
                    reader = csv.DictReader(aggregated)
                    row_dict = OrderedDict()
                    for row in reader:
                        for f in reader.fieldnames:
                            row_dict.update({f: row[f]})
                    return OrderedDict(row_dict)
