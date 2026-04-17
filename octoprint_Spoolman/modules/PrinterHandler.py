# coding=utf-8
from __future__ import absolute_import

import copy
import re
from octoprint.events import Events

from ..thirdparty.gcodeInterpreter import gcode
from ..common.settings import SettingsKeys

class PrinterHandler():
    def initialize(self):
        self.lastPrintCancelled = False
        self.lastPrintOdometer = None
        self.lastPrintOdometerLoad = None
        self.temperatureOverrides = {}
        self.bedTempOverride = None
        self.currentToolIdx = 0
        self._tempOverridesLoaded = False
        self._printActive = False

    def handlePrintingStatusChange(self, eventType):
        if eventType == Events.PRINT_STARTED:
            self.lastPrintCancelled = False
            self.lastPrintOdometer = gcode()
            self.lastPrintOdometerLoad = self.lastPrintOdometer._load(None)

            next(self.lastPrintOdometerLoad)

            self._printActive = True
            self._tempOverridesLoaded = False

        if eventType == Events.PRINT_CANCELLED:
            self.lastPrintCancelled = True

        if eventType == Events.PRINT_FAILED and self.lastPrintCancelled:
            # Ignore event, already handled while handling PRINT_CANCELLED
            return

        if (
            eventType == Events.PRINT_PAUSED or
            eventType == Events.PRINT_DONE or
            eventType == Events.PRINT_FAILED or
            eventType == Events.PRINT_CANCELLED
        ):
            self.commitSpoolUsage()

        if (
            eventType == Events.PRINT_DONE or
            eventType == Events.PRINT_FAILED or
            eventType == Events.PRINT_CANCELLED
        ):
            self.lastPrintOdometer = None
            self.lastPrintOdometerLoad = None
            self.temperatureOverrides = {}
            self.bedTempOverride = None
            self.currentToolIdx = 0
            self._tempOverridesLoaded = False
            self._printActive = False

    def handlePrintingGCode(self, command):
        if (
            not hasattr(self, "lastPrintOdometerLoad") or
            self.lastPrintOdometerLoad == None
        ):
            return

        peek_stats_helpers = self.lastPrintOdometerLoad.send(command)

    def commitSpoolUsage(self):
        peek_stats_helpers = self.lastPrintOdometerLoad.send(False)

        current_extrusion_stats = copy.deepcopy(peek_stats_helpers['get_current_extrusion_stats']())

        peek_stats_helpers['reset_extrusion_stats']()

        selectedSpoolIds = self._settings.get([SettingsKeys.SELECTED_SPOOL_IDS])

        for toolIdx, toolExtrusionLength in enumerate(current_extrusion_stats['extrusionAmount']):
            selectedSpool = None

            try:
                selectedSpool = selectedSpoolIds[str(toolIdx)]
            except:
                self._logger.info("Extruder '%s', spool id: none", toolIdx)

            if (
                not selectedSpool or
                selectedSpool.get('spoolId', None) == None
            ):
                continue

            selectedSpoolId = selectedSpool['spoolId']

            self._logger.info(
                "Extruder '%s', spool id: %s, usage: %s",
                toolIdx,
                selectedSpoolId,
                toolExtrusionLength
            )

            result = self.getSpoolmanConnector().handleCommitSpoolUsage(selectedSpoolId, toolExtrusionLength)

            if result.get('error', None):
                self.triggerPluginEvent(
                    Events.PLUGIN_SPOOLMAN_SPOOL_USAGE_ERROR,
                    result['error']
                )

                return

            self.triggerPluginEvent(
                Events.PLUGIN_SPOOLMAN_SPOOL_USAGE_COMMITTED,
                {
                    'toolIdx': toolIdx,
                    'spoolId': selectedSpoolId,
                    'extrusionLength': toolExtrusionLength,
                }
            )

    def _loadTemperatureOverrides(self):
        self.temperatureOverrides = {}
        self.bedTempOverride = None
        self.currentToolIdx = 0

        isEnabled = self._settings.get([SettingsKeys.IS_TEMPERATURE_OVERRIDE_ENABLED])
        if not isEnabled:
            return

        selectedSpoolIds = self._settings.get([SettingsKeys.SELECTED_SPOOL_IDS])
        if not selectedSpoolIds:
            return

        result = self.getSpoolmanConnector().handleGetSpoolsAvailable()

        if result.get('error', False):
            self._logger.warning("[Spoolman] Could not fetch spools for temperature override: %s", result.get('error'))
            return

        spoolsAvailable = result['data']['spools']
        maxBedTemp = None

        for toolIdxStr, spoolData in selectedSpoolIds.items():
            spoolId = spoolData.get('spoolId', None)
            if spoolId is None:
                continue

            spool = next(
                (s for s in spoolsAvailable if str(s['id']) == str(spoolId)),
                None
            )

            if not spool or 'filament' not in spool:
                continue

            filament = spool['filament']
            extruderTemp = filament.get('settings_extruder_temp', None)
            bedTemp = filament.get('settings_bed_temp', None)

            toolIdx = int(toolIdxStr)

            if extruderTemp is not None or bedTemp is not None:
                self.temperatureOverrides[toolIdx] = {
                    'extruder': extruderTemp,
                    'bed': bedTemp,
                }

            if bedTemp is not None:
                if maxBedTemp is None or bedTemp > maxBedTemp:
                    maxBedTemp = bedTemp

        self.bedTempOverride = maxBedTemp

        if self.temperatureOverrides:
            self._logger.info(
                "[Spoolman] Temperature overrides loaded: %s, bed override: %s",
                self.temperatureOverrides,
                self.bedTempOverride
            )

    def handleQueuingGCode(self, cmd, gcode):
        if not getattr(self, '_printActive', False):
            return None

        # Lazy-load temperature overrides on first relevant GCode command
        # to avoid race condition between event handler and GCode queue thread
        if not getattr(self, '_tempOverridesLoaded', False):
            self._loadTemperatureOverrides()
            self._tempOverridesLoaded = True

        if not self.temperatureOverrides and self.bedTempOverride is None:
            return None

        # Track tool changes
        if gcode and gcode.startswith('T'):
            try:
                self.currentToolIdx = int(gcode[1:])
            except (ValueError, IndexError):
                pass
            return None

        # Extruder temperature: M104 (set) / M109 (set and wait)
        if gcode in ('M104', 'M109'):
            return self._overrideExtruderTemp(cmd)

        # Bed temperature: M140 (set) / M190 (set and wait)
        if gcode in ('M140', 'M190'):
            return self._overrideBedTemp(cmd)

        return None

    def _overrideExtruderTemp(self, cmd):
        sMatch = re.search(r'S(\d+\.?\d*)', cmd)
        if not sMatch:
            return None

        currentTemp = float(sMatch.group(1))

        # Don't override S0 (heater off)
        if currentTemp == 0:
            return None

        tMatch = re.search(r'T(\d+)', cmd)
        toolIdx = int(tMatch.group(1)) if tMatch else getattr(self, 'currentToolIdx', 0)

        toolOverride = self.temperatureOverrides.get(toolIdx, None)
        if not toolOverride or toolOverride.get('extruder', None) is None:
            return None

        newTemp = toolOverride['extruder']
        newCmd = re.sub(r'S\d+\.?\d*', 'S' + str(newTemp), cmd)

        self._logger.info(
            "[Spoolman] Extruder temp override: %s -> %s (tool %d)",
            cmd.strip(), newCmd.strip(), toolIdx
        )

        return newCmd

    def _overrideBedTemp(self, cmd):
        if self.bedTempOverride is None:
            return None

        sMatch = re.search(r'S(\d+\.?\d*)', cmd)
        if not sMatch:
            return None

        currentTemp = float(sMatch.group(1))

        # Don't override S0 (heater off)
        if currentTemp == 0:
            return None

        newCmd = re.sub(r'S\d+\.?\d*', 'S' + str(self.bedTempOverride), cmd)

        self._logger.info(
            "[Spoolman] Bed temp override: %s -> %s",
            cmd.strip(), newCmd.strip()
        )

        return newCmd
