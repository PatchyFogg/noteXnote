from setuptools import setup

APP = ['notexnote.py']
DATA_FILES = ['HELP.html', 'progress.gif']
OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'noteXnote.icns',
    'packages': ['numpy', 'sounddevice', 'librosa', 'scipy',
                 'sklearn', 'soundfile', 'audioread', 'pooch',
                 'lazy_loader', 'soxr', 'msgpack', 'joblib',
                 'decorator', 'numba', '_soundfile_data'],
    'includes': ['sqlite3', '_sounddevice_data'],
    'frameworks': [],
    'plist': {
        'CFBundleName': 'noteXnote',
        'CFBundleDisplayName': 'noteXnote',
        'CFBundleIdentifier': 'com.notexnote.app',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'LSMinimumSystemVersion': '10.15',
        'NSHighResolutionCapable': True,
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
