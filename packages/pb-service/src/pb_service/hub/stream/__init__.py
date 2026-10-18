# SPDX-License-Identifier: MIT
# Copyright (c) 2026 The Pybricks Authors

"""What every hub reached over a byte stream needs, whatever carries it.

USB was the first such transport and RFCOMM is the second, but nothing in here
knows about either: a pipe with no message boundaries needs framing, and an
envelope to say which characteristic a message is about, and both are the same
whether the bytes came off a cable or a radio.
"""
