try:
    import loader_bootstrap
    print("Banana-Li: Installing binaries via ComfyUI Manager...")
    loader_bootstrap.ensure_binaries()
except ImportError:
    print("Banana-Li: loader_bootstrap.py not found, skipping binary installation.")
except Exception as e:
    print(f"Banana-Li: Failed to install binaries: {e}")
