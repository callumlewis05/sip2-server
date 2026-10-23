#!/usr/bin/env bash
# -*- coding: utf-8 -*-
#
# Copyright (C) 2026 CERN.
#
# CDS-SIP2 is free software; you can redistribute it and/or modify it under
# the terms of the MIT License; see LICENSE file for more details.

# Quit on errors
set -o errexit

# Quit on unbound symbols
set -o nounset

# Note: the tests use tests/fake_ils.py, so no docker services are needed.
# Any arguments are forwarded to "pytest" (the "${@+...}" form also works
# on the old bash 3.2 shipped with macOS when no arguments are given).
python -m pytest ${@+"$@"}
tests_exit_code=$?
exit "$tests_exit_code"