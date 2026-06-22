"""ISP Ticket Formatter — legacy desktop entry point (tkinter MVC).

The web app (``main.py``) is now the primary front end. This launches the
original tkinter desktop app, which auto-copies the formatted ticket to the
clipboard on paste. Same domain layer (``model/``); Windows-only rich clipboard.

Run:  python view_app.py
"""

from model import TicketModel
from view import AppView
from controller import AppController
from services import ClipboardService


def main():
    model = TicketModel()
    view = AppView()
    clipboard = ClipboardService()
    AppController(model, view, clipboard)  # subscribes to the view's events
    view.mainloop()


if __name__ == "__main__":
    main()