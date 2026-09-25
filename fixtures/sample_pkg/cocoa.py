"""A PyObjC app delegate: Cocoa calls methods by selector, and a selector is
a string.

`quit_` is the Python spelling of the Objective-C selector `quit:` — each
colon becomes an underscore. Nothing in Python calls `quit_`; the menu item
is wired to the string "quit:" and Cocoa sends it. On the first PyObjC app
spanda read, about fifty such methods sat on the "no explanation" list.
"""

import objc
from AppKit import NSMenuItem, NSObject, NSStatusBar


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Quit", "quit:", "q")
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)
        self.status.menu().addItem_(item)

    def awakeFromNib(self):
        pass

    def quit_(self, sender):
        pass

    @objc.IBAction
    def refresh_(self, sender):
        pass

    @objc.python_method
    def helper(self):
        """Marked as not a selector: an ordinary method Cocoa cannot reach."""
        return 1
