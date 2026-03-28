
import sys
import os

try:
    import toml
    print(f"toml version: {toml.__version__}")
except ImportError:
    print("toml not installed")

try:
    import tomllib
    print("tomllib is available (Python 3.11+ likely)")
except ImportError:
    print("tomllib is NOT available")

filename = "test_reproduce.toml"

with open(filename, "w", encoding="utf-8") as f:
    f.write('title = "TOML Example"\n')

print(f"Testing with 'toml' package using 'rb' mode...")
try:
    if 'toml' in sys.modules:
        with open(filename, "rb") as f:
            try:
                data = toml.load(f)
                print("Success with toml.load(binary_file)")
            except Exception as e:
                print(f"Failed with toml.load(binary_file): {type(e).__name__} {e}")
except Exception as e:
    print(f"Outer error: {e}")

print(f"Testing with 'tomllib' package (if available) using 'rb' mode...")
try:
    if 'tomllib' in sys.modules:
        with open(filename, "rb") as f:
            try:
                data = tomllib.load(f)
                print("Success with tomllib.load(binary_file)")
            except Exception as e:
                print(f"Failed with tomllib.load(binary_file): {type(e).__name__} {e}")
except Exception as e:
    print(f"Outer error: {e}")

if os.path.exists(filename):
    os.remove(filename)
