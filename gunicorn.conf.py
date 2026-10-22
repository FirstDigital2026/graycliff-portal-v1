"""One app worker owns intake and the scheduler; never start threads before forking."""
workers = 1
threads = 2
preload_app = False
timeout = 120
graceful_timeout = 120
