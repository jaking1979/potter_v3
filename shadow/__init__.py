"""Shadow-MVP runtime pieces: effect gate, brokered model calls, cost log, turn loop.

Network-free by construction: every transport (Telegram, model provider, Home
Assistant, mail) is an injected callable supplied by the install wiring.
"""
