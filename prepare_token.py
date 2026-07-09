from kis_overseas import KisApiError, KisOverseasClient
from token_manager import prepare_access_token


def main():
    client = KisOverseasClient()
    prepare_access_token(client)


if __name__ == "__main__":
    try:
        main()
    except KisApiError as error:
        print(f"[KIS_API_FAILED] {error}")
        raise
