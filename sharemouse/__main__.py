"""Launch the platform desktop; the former Tk view is a development fixture."""
from .client import current_client, launch


def __getattr__(name):
    if name == 'App':
        from .legacy_tk import App
        return App
    raise AttributeError(name)


def main(client=None):
    launch(client or current_client())


if __name__ == '__main__':
    main()
