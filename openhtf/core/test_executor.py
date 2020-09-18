# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""TestExecutor executes tests."""

from __future__ import google_type_annotations

import logging
import pstats
import sys
import tempfile
import threading
import traceback
from typing import List, Optional, Sequence, Text, Type, TYPE_CHECKING, Union

from openhtf.core import base_plugs
from openhtf.core import diagnoses_lib
from openhtf.core import phase_descriptor
from openhtf.core import phase_executor
from openhtf.core import phase_group
from openhtf.core import test_record
from openhtf.core import test_state
from openhtf.util import conf
from openhtf.util import threads

if TYPE_CHECKING:
  from openhtf.core import test_descriptor  # pylint: disable=g-import-not-at-top

_LOG = logging.getLogger(__name__)

conf.declare(
    'cancel_timeout_s',
    default_value=2,
    description='Timeout (in seconds) when the test has been cancelled'
    'to wait for the running phase to exit.')

conf.declare(
    'stop_on_first_failure',
    default_value=False,
    description='Stop current test execution and return Outcome FAIL'
    'on first phase with failed measurement.')


class TestExecutionError(Exception):
  """Raised when there's an internal error during test execution."""


class TestStopError(Exception):
  """Test is being stopped."""


def combine_profile_stats(profile_stats_iter: List[pstats.Stats],
                          output_filename: Text) -> None:
  """Given an iterable of pstats.Stats, combine them into a single Stats."""
  profile_stats_filenames = []
  for profile_stats in profile_stats_iter:
    with tempfile.NamedTemporaryFile(delete=False) as f:
      profile_stats_filename = f.name
    profile_stats.dump_stats(profile_stats_filename)
    profile_stats_filenames.append(profile_stats_filename)
  if profile_stats_filenames:
    pstats.Stats(*profile_stats_filenames).dump_stats(output_filename)


# pylint: disable=too-many-instance-attributes
class TestExecutor(threads.KillableThread):
  """Encompasses the execution of a single test."""
  daemon = True

  def __init__(self, test_descriptor: 'test_descriptor.TestDescriptor',
               execution_uid: Text,
               test_start: Optional[phase_descriptor.PhaseDescriptor],
               test_options: 'test_descriptor.TestOptions',
               run_with_profiling: bool):
    super(TestExecutor, self).__init__(
        name='TestExecutorThread', run_with_profiling=run_with_profiling)
    self.test_state = None  # type: Optional[test_state.TestState]

    self._test_descriptor = test_descriptor
    self._test_start = test_start
    self._test_options = test_options
    self._lock = threading.Lock()
    self._phase_exec = None  # type: Optional[phase_executor.PhaseExecutor]
    self.uid = execution_uid
    self._last_outcome = None  # type: Optional[phase_executor.PhaseExecutionOutcome]
    self._abort = threading.Event()
    self._full_abort = threading.Event()
    self._teardown_phases_lock = threading.Lock()
    # Populated if profiling is enabled.
    self._phase_profile_stats = []  # type: List[pstats.Stats]

  @property
  def phase_profile_stats(self) -> List[pstats.Stats]:
    """Returns iterable of profiling Stats objects, per phase."""
    return self._phase_profile_stats

  def close(self) -> None:
    """Close and remove any global registrations.

    Always call this function when finished with this instance.

    This function is defined instead of a __del__ function because Python calls
    the __del__ function unreliably.
    """
    self.wait()
    self.test_state.close()

  def abort(self) -> None:
    """Abort this test."""
    if self._abort.is_set():
      _LOG.error('Abort already set; forcibly stopping the process.')
      self._full_abort.set()
      self._stop_phase_executor(force=True)
      return
    _LOG.error('Abort test executor.')
    # Deterministically mark the test as aborted.
    self._abort.set()
    self._stop_phase_executor()
    # No need to kill this thread because the abort state has been set, it will
    # end as soon as all queued teardown phases are run.

  def finalize(self) -> test_state.TestState:
    """Finalize test execution and output resulting record to callbacks.

    Should only be called once at the conclusion of a test run, and will raise
    an exception if end_time_millis is already set.

    Returns:
      Finalized TestState.  It must not be modified after this call.

    Raises:
      TestStopError: test
      TestAlreadyFinalized if end_time_millis already set.
    """
    if not self.test_state:
      raise TestStopError('Test Stopped.')
    if self.test_state.test_record.dut_id is None:
      _LOG.warning('DUT ID is still not set; using default.')
      self.test_state.test_record.dut_id = self._test_options.default_dut_id

    return self.test_state

  def wait(self) -> None:
    """Waits until death."""
    # Must use a timeout here in case this is called from the main thread.
    # Otherwise, the SIGINT abort logic in test_descriptor will not get called.
    timeout = 31557600  # Seconds in a year.
    if sys.version_info >= (3, 2):
      # TIMEOUT_MAX can be too large and cause overflows on 32-bit OSes, so take
      # whichever timeout is shorter.
      timeout = min(threading.TIMEOUT_MAX, timeout)  # pytype: disable=module-attr
    self.join(timeout)

  def _thread_proc(self) -> None:
    """Handles one whole test from start to finish."""
    try:
      # Top level steps required to run a single iteration of the Test.
      self.test_state = test_state.TestState(self._test_descriptor, self.uid,
                                             self._test_options)
      phase_exec = phase_executor.PhaseExecutor(self.test_state)

      # Any access to self._exit_stacks must be done while holding this lock.
      with self._lock:
        self._phase_exec = phase_exec

      if self._test_start is not None and self._execute_test_start():
        # Exit early if test_start returned a terminal outcome of any kind.
        return
      self.test_state.mark_test_started()

      # Full plug initialization happens _after_ the start trigger, as close to
      # test execution as possible, for the best chance of test equipment being
      # in a known-good state at the start of test execution.
      if self._initialize_plugs():
        return

      # Everything is set, set status and begin test execution.
      self.test_state.set_status_running()
      self._execute_phase_group(self._test_descriptor.phase_group)
      self._execute_test_diagnosers()
    except:  # pylint: disable=bare-except
      stacktrace = traceback.format_exc()
      _LOG.error('Error in TestExecutor: \n%s', stacktrace)
      raise
    finally:
      self._execute_test_teardown()

  def _initialize_plugs(
      self,
      plug_types: Optional[List[Type[base_plugs.BasePlug]]] = None) -> bool:
    """Initialize plugs.

    Args:
      plug_types: optional list of plug classes to initialize.

    Returns:
      True if there was an error initializing the plugs.
    """
    try:
      self.test_state.plug_manager.initialize_plugs(plug_types=plug_types)
      return False
    except Exception:  # pylint: disable=broad-except
      # Record the equivalent failure outcome and exit early.
      self._last_outcome = phase_executor.PhaseExecutionOutcome(
          phase_executor.ExceptionInfo(*sys.exc_info()))
      return True

  def _execute_test_start(self) -> bool:
    """Run the start trigger phase, and check that the DUT ID is set after.

    Initializes any plugs used in the trigger.
    Logs a warning if the start trigger failed to set the DUT ID.

    The test start is special because we wait to initialize all other plugs
    until this phase runs.

    Returns:
      True if there was a terminal error either setting up or running the test
      start phase.
    """
    # Have the phase executor run the start trigger phase. Do partial plug
    # initialization for just the plugs needed by the start trigger phase.
    if self._initialize_plugs(
        plug_types=[phase_plug.cls for phase_plug in self._test_start.plugs]):
      return True

    outcome, profile_stats = self._phase_exec.execute_phase(
        self._test_start, self._run_with_profiling)

    if profile_stats is not None:
      self._phase_profile_stats.append(profile_stats)

    if outcome.is_terminal:
      self._last_outcome = outcome
      return True

    if self.test_state.test_record.dut_id is None:
      _LOG.warning('Start trigger did not set a DUT ID.')
    return False

  def _stop_phase_executor(self, force: bool = False) -> None:
    with self._lock:
      phase_exec = self._phase_exec
      if not phase_exec:
        # The test executor has not started yet, so no stopping is required.
        return
    if not force and not self._teardown_phases_lock.acquire(False):
      # If locked, teardown phases are running, so do not cancel those.
      return
    try:
      phase_exec.stop(timeout_s=conf.cancel_timeout_s)
      # Resetting so phase_exec can run teardown phases.
      phase_exec.reset_stop()
    finally:
      if not force:
        self._teardown_phases_lock.release()

  def _execute_test_teardown(self) -> None:
    # Plug teardown does not affect the test outcome.
    self.test_state.plug_manager.tear_down_plugs()

    # Now finalize the test state.
    if self._abort.is_set():
      self.test_state.state_logger.debug('Finishing test with outcome ABORTED.')
      self.test_state.abort()
    elif self._last_outcome and self._last_outcome.is_terminal:
      self.test_state.finalize_from_phase_outcome(self._last_outcome)
    else:
      self.test_state.finalize_normally()

  def _handle_phase(
      self, phase: Union[phase_group.PhaseGroup,
                         phase_descriptor.PhaseDescriptor]
  ) -> bool:
    if isinstance(phase, phase_group.PhaseGroup):
      return self._execute_phase_group(phase)

    self.test_state.state_logger.debug('Handling phase %s', phase.name)
    outcome, profile_stats = self._phase_exec.execute_phase(
        phase, self._run_with_profiling)

    if profile_stats is not None:
      self._phase_profile_stats.append(profile_stats)

    if (self.test_state.test_options.stop_on_first_failure or
        conf.stop_on_first_failure):
      # Stop Test on first measurement failure
      current_phase_result = self.test_state.test_record.phases[
          len(self.test_state.test_record.phases) - 1]
      if current_phase_result.outcome == test_record.PhaseOutcome.FAIL:
        outcome = phase_executor.PhaseExecutionOutcome(
            phase_descriptor.PhaseResult.STOP)
        self.test_state.state_logger.error(
            'Stopping test because stop_on_first_failure is True')

    if outcome.is_terminal and not self._last_outcome:
      self._last_outcome = outcome

    return outcome.is_terminal

  def _execute_abortable_phases(self, type_name: Text, phases: Sequence[Union[
      phase_descriptor.PhaseDescriptor, phase_group.PhaseGroup]],
                                group_name: Optional[Text]) -> bool:
    """Execute phases, returning immediately if any error or abort is triggered.

    Args:
      type_name: str, type of phases running, usually 'Setup' or 'Main'.
      phases: iterable of phase_descriptor.Phase or phase_group.PhaseGroup
        instances, the phases to execute.
      group_name: str or None, name of the executing group.

    Returns:
      True if there is a terminal error or the test is aborted, False otherwise.
    """
    if group_name and phases:
      self.test_state.state_logger.debug('Executing %s phases for %s',
                                         type_name, group_name)
    for phase in phases:
      if self._abort.is_set() or self._handle_phase(phase):
        return True
    return False

  def _execute_teardown_phases(self, teardown_phases: Sequence[Union[
      phase_descriptor.PhaseDescriptor, phase_group.PhaseGroup]],
                               group_name: Text) -> bool:
    """Execute all the teardown phases, regardless of errors.

    Args:
      teardown_phases: iterable of phase_descriptor.Phase or
        phase_group.PhaseGroup instances, the phases to execute.
      group_name: str or None, name of the executing group.

    Returns:
      True if there is at least one terminal error, False otherwise.
    """
    if group_name and teardown_phases:
      self.test_state.state_logger.debug('Executing teardown phases for %s',
                                         group_name)
    ret = False
    with self._teardown_phases_lock:
      for teardown_phase in teardown_phases:
        if self._full_abort.is_set():
          ret = True
          break
        if self._handle_phase(teardown_phase):
          ret = True
    return ret

  def _execute_phase_group(self, group: phase_group.PhaseGroup) -> bool:
    """Executes the phases in a phase group.

    This will run the phases in the phase group, ensuring if the setup
    phases all run without error that the teardown phases will also run, no
    matter the errors during the main phases.

    This function is recursive.  Do not construct phase groups that contain
    themselves.

    Args:
      group: phase_group.PhaseGroup, the phase group to execute.

    Returns:
      True if the phases are terminal; otherwise returns False.
    """
    if group.name:
      self.test_state.state_logger.debug('Entering PhaseGroup %s', group.name)
    if self._execute_abortable_phases('setup', group.setup, group.name):
      return True
    main_ret = self._execute_abortable_phases('main', group.main, group.name)
    teardown_ret = self._execute_teardown_phases(group.teardown, group.name)
    return main_ret or teardown_ret

  def _execute_test_diagnoser(
      self, diagnoser: diagnoses_lib.BaseTestDiagnoser) -> None:
    try:
      self.test_state.diagnoses_manager.execute_test_diagnoser(
          diagnoser, self.test_state.test_record)
    except Exception:  # pylint: disable=broad-except
      if self._last_outcome and self._last_outcome.is_terminal:
        self.test_state.state_logger.exception(
            'Test Diagnoser %s raised an exception, but the test outcome is '
            'already terminal; logging additional exception here.',
            diagnoser.name)
      else:
        # Record the equivalent failure outcome and exit early.
        self._last_outcome = phase_executor.PhaseExecutionOutcome(
            phase_executor.ExceptionInfo(*sys.exc_info()))

  def _execute_test_diagnosers(self) -> None:
    for diagnoser in self._test_options.diagnosers:
      self._execute_test_diagnoser(diagnoser)
