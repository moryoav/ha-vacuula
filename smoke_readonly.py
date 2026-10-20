"""Verify an existing Vacuula server without sending any movement or settings action."""

import argparse
import asyncio
import json
from contextlib import aclosing

import aiohttp

from custom_components.vacuula.client import VacuulaClient, unwrap


async def check(url: str) -> None:
    """Verify REST reads and event streams, printing telemetry without robot writes."""
    async with aiohttp.ClientSession() as session:
        client = VacuulaClient(session, url)
        info = await client.async_server_info()
        robots = await client.async_robots()
        result = {
            "server_version": info.get("version"),
            "robot_count": len(robots),
            "robots": [],
        }
        async with aclosing(client.async_server_events()) as events:
            async with asyncio.timeout(15):
                snapshot = await anext(events)
            assert set(snapshot) == set(robots)
            result["server_events"] = "ok"
        for serial, metadata in robots.items():
            robot = {"generation": metadata["type"], "connected": metadata["connected"]}
            robot["map_count"] = len(await client.async_maps(serial))
            robot["run_count"] = len(await client.async_runs(serial))
            if metadata["connected"]:
                async with aclosing(client.async_robot_events(serial)) as events:
                    async with asyncio.timeout(15):
                        assert (await anext(events))["_vacuula_stream_opened"]
                    robot["robot_events"] = "ok"
                    state = await client.async_robot_state(serial, metadata["type"])
                    robot["state"] = state.get("state")
                    robot["error"] = state.get("error", state.get("errors"))
                    details = state.get("details", {})
                    robot["battery"] = details.get("charge")
                    robot["docked"] = details.get("isDocked", details.get("is_docked"))
                for command in (
                    (
                        {"cmd": "getGeneralInfo"},
                        {"cmd": "getPreferences"},
                        {"cmd": "getSchedule"},
                    )
                    if metadata["type"] == "neato.gen3"
                    else (
                        {"ability": "settings.cleaning_options"},
                        {"ability": "scheduling.show"},
                    )
                ):
                    response = unwrap(await client.async_command(serial, command))
                    robot[next(iter(command.values()))] = "ok" if response else "empty"
            result["robots"].append(robot)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    asyncio.run(check(parser.parse_args().url))
