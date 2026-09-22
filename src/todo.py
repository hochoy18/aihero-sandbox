"""A tiny todo item module."""

class TodoItem:
    def __init__(self, id, title, done=False):
        self.id = id
        self.title = title
        self.done = done

    def complete(self):
        self.done = True

    def reopen(self):
        self.done = False
