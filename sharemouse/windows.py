"""Dedicated Windows client entry point."""
from .client import WINDOWS, launch


def main():
    launch(WINDOWS)


if __name__ == '__main__':
    main()
