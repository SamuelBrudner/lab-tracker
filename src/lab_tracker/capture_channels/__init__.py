"""Server-side capture channels: observe research activity where no client runs.

Every channel here lands its captures as STAGED notes carrying pointers and
bounded text, is off until an operator configures it, and attributes each note
honestly: a person who acted (a Slack save, a verified email) authors their
capture; a record the server fetched on its own (a calendar booking, a new file
in a registered store) is authored by the ``SYSTEM`` principal and labelled with
its ``capture_channel``. See ``docs/server-capture-channels.md``.
"""
