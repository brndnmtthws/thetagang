class Portfolio:
    def __init__(self, ib):
        self.ib = ib

    def manage(self, account_summary, portfolio):
        self.check_puts()
        self.check_calls()

    def check_puts(self):
        # Check for puts which may be rolled to the next expiration or a better price
        print("hi")

    def check_calls(self):
        # Check for calls which may be rolled to the next expiration or a better price
        print("hi")
