def load_env(filepath=".env"):
    env = {}

    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()

                if not line or line.startswith("#"):
                    continue

                if "=" not in line:
                    continue

                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                env[key] = val

        print(f"Loaded credentials from {filepath}")

    except FileNotFoundError:
        print(f"env file not found at '{filepath}'")
        exit(1)

    return env


env = load_env(".env")

BASE_URL = env.get("BASE_URL", "http://mysite.localhost:8080")
API_KEY = env.get("ERP_API_KEY", "")
API_SECRET = env.get("ERP_API_SECRET", "")

if not API_KEY or not API_SECRET:
    print(" ERP_API_KEY or ERP_API_SECRET missing in .env file")
    exit(1)