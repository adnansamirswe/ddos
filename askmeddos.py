import asyncio
import aiohttp
import random
import string
import time
import os
import json
from urllib.parse import urljoin
import argparse
import backoff
import sys
import psutil  # Add this to monitor system resources (pip install psutil)
from termcolor import colored

# Command line arguments
parser = argparse.ArgumentParser(description='Load testing tool for your own infrastructure')
parser.add_argument('--url', default="https://errorx.net/", help='Target URL (your own website)')
parser.add_argument('--connections', type=int, default=20000, help='Max concurrent connections')  # Drastically increased
parser.add_argument('--timeout', type=int, default=10, help='Request timeout in seconds')  # Reduced for faster cycling
parser.add_argument('--debug', action='store_true', help='Enable detailed error logging')
parser.add_argument('--duration', type=int, default=0, help='Test duration in seconds (0 = unlimited)')
parser.add_argument('--ramp', type=int, default=10, help='Ramp up time in seconds (reduced)')  # Much faster ramp-up
parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for requests')  # Drastically increased
parser.add_argument('--connection-ramp', type=int, default=2000, help='How many connections to open in each batch')  # Increased
parser.add_argument('--aggressive', action='store_true', help='Enable aggressive mode (faster but may overwhelm client)')
parser.add_argument('--max-response-store', type=int, default=1000, help='Maximum number of response times to store (to limit memory usage)')
parser.add_argument('--cpu-limit', type=float, default=85.0, help='CPU usage percentage limit (will slow down if exceeded)')
parser.add_argument('--memory-limit', type=float, default=80.0, help='Memory usage percentage limit (will slow down if exceeded)')
parser.add_argument('--ultra', action='store_true', help='Enable ultra-aggressive mode (maximum throughput)')
parser.add_argument('--no-verify', action='store_true', help='Disable response verification for highest throughput')
parser.add_argument('--workers', type=int, default=0, help='Number of worker processes (0=auto, uses all CPUs)')
parser.add_argument('--non-interactive', action='store_true', help='Skip interactive setup even if no mode is specified')
args = parser.parse_args()

# Interactive setup function
def interactive_setup():
    """Interactive configuration when no mode arguments are provided"""
    print(colored("\n===== LOAD TEST INTERACTIVE SETUP =====", "cyan"))
    print(colored("Select the testing mode you want to use:", "cyan"))
    print("1. Standard Mode   - Balanced testing with accurate metrics")
    print("2. Aggressive Mode - High performance with less validation")
    print("3. Ultra Mode     - Maximum throughput, fire-and-forget")

    # Get testing mode
    while True:
        try:
            mode_choice = input(colored("\nEnter your choice (1-3): ", "green"))
            mode = int(mode_choice)
            if mode not in [1, 2, 3]:
                print(colored("Invalid choice. Please enter 1, 2, or 3.", "red"))
                continue
            break
        except ValueError:
            print(colored("Invalid input. Please enter a number.", "red"))
    
    # Get target URL
    default_url = "https://errorx.net"
    url = input(colored(f"\nEnter target URL (default {default_url}): ", "green"))
    if not url:
        url = default_url
    
    # Get connections
    connections_map = {1: 1000, 2: 5000, 3: 10000}
    default_connections = connections_map[mode]
    connections_input = input(colored(f"\nNumber of connections (default {default_connections}): ", "green"))
    connections = int(connections_input) if connections_input else default_connections
    
    # Get test duration
    duration_input = input(colored("\nTest duration in seconds (0 for unlimited): ", "green"))
    duration = int(duration_input) if duration_input else 0
    
    # Get worker count if in ultra mode
    workers = 0
    if mode == 3:
        cpu_count = os.cpu_count() or 1
        workers_input = input(colored(f"\nNumber of worker processes (0 for auto detection, default {cpu_count} detected CPUs): ", "green"))
        workers = int(workers_input) if workers_input else cpu_count
    
    # Configure settings based on mode
    config = {
        "url": url,
        "connections": connections,
        "duration": duration,
        "aggressive": mode >= 2,  # Aggressive or Ultra
        "ultra": mode == 3,       # Ultra only
        "workers": workers,
        "batch_size": {1: 100, 2: 500, 3: 1000}.get(mode, 100),
        "timeout": {1: 30, 2: 10, 3: 5}.get(mode, 30),
        "ramp": {1: 30, 2: 10, 3: 5}.get(mode, 30) if connections > 1000 else 0,
        "no_verify": mode == 3,
    }
    
    print(colored("\n===== CONFIGURATION SUMMARY =====", "cyan"))
    print(f"Mode:        {'Standard' if mode == 1 else 'Aggressive' if mode == 2 else 'Ultra'}")
    print(f"URL:         {config['url']}")
    print(f"Connections: {config['connections']}")
    print(f"Duration:    {'Unlimited' if config['duration'] == 0 else f'{config['duration']} seconds'}")
    if mode == 3:
        print(f"Workers:     {'Auto-detect' if config['workers'] == 0 else config['workers']}")
    
    confirm = input(colored("\nProceed with this configuration? (Y/n): ", "green"))
    if confirm.lower() == 'n':
        print(colored("Setup cancelled. Exiting...", "red"))
        sys.exit(0)
    
    return config

# Check if we need interactive setup
if not (args.aggressive or args.ultra or args.non_interactive) and len(sys.argv) == 1:
    try:
        config = interactive_setup()
        # Apply the interactive configuration
        args.url = config["url"]
        args.connections = config["connections"]
        args.duration = config["duration"]
        args.aggressive = config["aggressive"]
        args.ultra = config["ultra"]
        args.workers = config["workers"]
        args.batch_size = config["batch_size"]
        args.timeout = config["timeout"]
        args.ramp = config["ramp"]
        args.no_verify = config["no_verify"]
        
        print(colored("\nStarting load test with your configuration...\n", "green"))
    except KeyboardInterrupt:
        print(colored("\nSetup cancelled. Exiting...", "red"))
        sys.exit(0)

# Target site configuration
TARGET_URL = args.url
MAX_CONNECTIONS = args.connections
REQUEST_TIMEOUT = args.timeout
DEBUG_MODE = args.debug
TEST_DURATION = args.duration
RAMP_UP_TIME = args.ramp
BATCH_SIZE = min(args.batch_size, MAX_CONNECTIONS)
CONNECTION_RAMP = args.connection_ramp
AGGRESSIVE_MODE = args.aggressive
MAX_RESPONSE_STORE = args.max_response_store
CPU_LIMIT = args.cpu_limit
MEMORY_LIMIT = args.memory_limit
ULTRA_MODE = args.ultra
NO_VERIFY = args.no_verify or ULTRA_MODE  # Ultra mode implies no verification
WORKERS = args.workers if args.workers > 0 else os.cpu_count() or 1

# Realistic browser headers
HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"},
]

# Add actual endpoints from your site
# ENDPOINTS = ["", "about", "contact", "signup", "login", "pricing"]

ENDPOINTS = [""]

# Statistics tracking with memory optimization
stats = {
    "requests_sent": 0,
    "success": 0,
    "failures": 0,
    "start_time": None,
    "status_codes": {},
    "response_times": [],
    "error_types": {},
    "bandwidth_used": 0,
    "min_response_time": float('inf'),
    "max_response_time": 0,
    "sum_response_time": 0,  # For calculating average without storing all times
    "host_cpu_usage": [],
    "host_memory_usage": []
}

# Generate random data for requests - more minimal for speed
def generate_random_string(length=4):  # Reduced length for speed
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# Simplified URL generation for maximum speed
def generate_random_url():
    # In ultra mode, don't use varied endpoints or params at all for maximum throughput
    if ULTRA_MODE:
        return TARGET_URL
    endpoint = random.choice(ENDPOINTS)
    # In aggressive mode, don't add random params to most requests for speed
    if not AGGRESSIVE_MODE or random.random() > 0.9:  # Only 10% get params in aggressive mode
        param = generate_random_string(4)
        return urljoin(TARGET_URL, endpoint) + f"?r={param}"
    return urljoin(TARGET_URL, endpoint)

# Limited storage for response times to avoid memory issues
def record_response_time(time_value):
    stats["sum_response_time"] += time_value
    stats["min_response_time"] = min(stats["min_response_time"], time_value)
    stats["max_response_time"] = max(stats["max_response_time"], time_value)
    
    # Keep a limited sample of response times to avoid memory bloat
    if len(stats["response_times"]) < MAX_RESPONSE_STORE:
        stats["response_times"].append(time_value)
    elif random.random() < 0.1:  # 10% chance to replace an old value
        index = random.randint(0, len(stats["response_times"]) - 1)
        stats["response_times"][index] = time_value

# Get current system resource usage
def get_system_usage():
    return {
        "cpu": psutil.cpu_percent(interval=0.1),
        "memory": psutil.virtual_memory().percent,
        "connections": len(psutil.net_connections())
    }

@backoff.on_exception(backoff.expo, 
                     (aiohttp.ClientError, asyncio.TimeoutError),
                     max_tries=3,
                     max_time=30)
async def send_request_with_retry(session, url, method, headers, payload=None):
    try:
        if method == "GET":
            return await session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        else:  # POST
            return await session.post(url, data=payload, headers=headers, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        if DEBUG_MODE:
            print(f"Retry exception: {type(e).__name__}: {str(e)}")
        raise

# Modified send_request for maximum throughput
async def send_request(session, sem, request_id, current_connections):
    async with sem:
        # In ultra mode, always use the same URL with no params
        url = TARGET_URL if ULTRA_MODE else generate_random_url()
        
        # In ultra/aggressive mode, use minimal headers for speed
        if ULTRA_MODE:
            # Bare minimum headers
            headers = {"User-Agent": "Mozilla/5.0"}
        elif AGGRESSIVE_MODE:
            headers = {"User-Agent": "Mozilla/5.0"}
        else:
            headers = random.choice(HEADERS_LIST).copy()
        
        # In aggressive mode, always use GET for maximum throughput
        method = "GET" if AGGRESSIVE_MODE else random.choices(["GET", "POST"], weights=[0.95, 0.05])[0]
        payload = None
        if method == "POST" and not AGGRESSIVE_MODE:
            payload = {"q": generate_random_string(5)}
        
        start_time = time.time()
        try:
            if DEBUG_MODE:
                print(f"Sending GET request to {url}")
            
            # Ultra mode: fire and forget, don't even wait for response
            if ULTRA_MODE:
                # Use a non-blocking request that we don't wait for
                task = asyncio.create_task(session.get(url, headers=headers, timeout=REQUEST_TIMEOUT))
                
                # Record stats without waiting for completion
                stats["requests_sent"] += 1
                stats["success"] += 1
                response_time = time.time() - start_time
                record_response_time(response_time)
                
                return True
                
            elif AGGRESSIVE_MODE:
                async with session.get(url, headers=headers, timeout=REQUEST_TIMEOUT) as response:
                    response_time = time.time() - start_time
                    
                    # Skip reading response in aggressive mode
                    stats["requests_sent"] += 1
                    stats["success"] += 1
                    status = response.status
                    stats["status_codes"][status] = stats["status_codes"].get(status, 0) + 1
                    record_response_time(response_time)
                    
                    return True
            else:
                async with await send_request_with_retry(session, url, method, headers, payload) as response:
                    response_time = time.time() - start_time
                    
                    # Memory-efficient response handling
                    response_len = 0
                    chunk_size = 8192  # Read in chunks to limit memory usage
                    
                    async for chunk in response.content.iter_chunked(chunk_size):
                        response_len += len(chunk)
                        # Don't store the actual response data - just count its size
                    
                    # Update statistics with memory efficiency
                    stats["requests_sent"] += 1
                    stats["success"] += 1
                    status = response.status
                    stats["status_codes"][status] = stats["status_codes"].get(status, 0) + 1
                    record_response_time(response_time)
                    stats["bandwidth_used"] += response_len
                    
                    if DEBUG_MODE:
                        print(f"Request {request_id}: {method} {url} -> {status} ({response_time*1000:.2f}ms, {response_len} bytes)")
                    
                    return True
                
        except Exception as e:
            # In aggressive mode, minimize error processing for speed
            stats["requests_sent"] += 1
            stats["failures"] += 1
            
            if not AGGRESSIVE_MODE:
                # Detailed error categorization for non-aggressive mode
                if isinstance(e, aiohttp.ClientConnectorError):
                    error_type = "connection_error"
                elif isinstance(e, aiohttp.ClientResponseError):
                    error_type = f"http_error_{e.status}"
                elif isinstance(e, asyncio.TimeoutError):
                    error_type = "timeout"
                else:
                    error_type = type(e).__name__
                    
                stats["error_types"][error_type] = stats["error_types"].get(error_type, 0) + 1
            
            if DEBUG_MODE:
                print(f"Error on {url}: {type(e).__name__}: {str(e)}")
            
            return False

# Resource monitoring task
async def monitor_resources():
    while True:
        usage = get_system_usage()
        stats["host_cpu_usage"].append(usage["cpu"])
        stats["host_memory_usage"].append(usage["memory"])
        
        # Keep only the last 10 measurements
        if len(stats["host_cpu_usage"]) > 10:
            stats["host_cpu_usage"] = stats["host_cpu_usage"][-10:]
        if len(stats["host_memory_usage"]) > 10:
            stats["host_memory_usage"] = stats["host_memory_usage"][-10:]
            
        # Check if we're reaching resource limits
        if usage["cpu"] > CPU_LIMIT:
            print(colored(f"⚠️ WARNING: CPU usage at {usage['cpu']}% (limit: {CPU_LIMIT}%)", "yellow"))
        if usage["memory"] > MEMORY_LIMIT:
            print(colored(f"⚠️ WARNING: Memory usage at {usage['memory']}% (limit: {MEMORY_LIMIT}%)", "yellow"))
            
        await asyncio.sleep(5)

async def print_stats():
    last_requests = 0
    last_time = time.time()
    
    while True:
        await asyncio.sleep(5)
        
        if not stats["start_time"]:
            continue
        
        current_time = time.time()
        elapsed = current_time - stats["start_time"]
        
        # Calculate rates for last interval
        interval_requests = stats["requests_sent"] - last_requests
        interval_time = current_time - last_time
        current_rps = interval_requests / interval_time if interval_time > 0 else 0
        
        # Overall statistics - calculate avg without summing the array
        requests_per_second = stats["requests_sent"] / elapsed if elapsed > 0 else 0
        avg_response_time = stats["sum_response_time"] / stats["requests_sent"] if stats["requests_sent"] > 0 else 0
        
        # Update for next interval calculation
        last_requests = stats["requests_sent"]
        last_time = current_time
        
        # Format bandwidth
        bandwidth_mb = stats["bandwidth_used"] / (1024 * 1024)
        bandwidth_rate = bandwidth_mb / elapsed if elapsed > 0 else 0
        
        # Get resource usage
        usage = get_system_usage()
        
        print(f"\n--- STATISTICS AFTER {elapsed:.1f} SECONDS ---")
        print(colored(f"Total Requests: {stats['requests_sent']}", "cyan"))
        print(f"Requests/second: {requests_per_second:.2f} (overall) | {current_rps:.2f} (current)")
        print(colored(f"Success: {stats['success']} | Failures: {stats['failures']}", "green" if stats["failures"] == 0 else "yellow"))
        print(f"Success rate: {(stats['success']/stats['requests_sent']*100) if stats['requests_sent'] > 0 else 0:.1f}%")
        print(f"Response time: {avg_response_time*1000:.2f}ms avg, {stats['min_response_time']*1000:.2f}ms min, {stats['max_response_time']*1000:.2f}ms max")
        print(f"Bandwidth: {bandwidth_mb:.2f} MB ({bandwidth_rate:.2f} MB/s)")
        print(colored(f"Status codes: {stats['status_codes']}", "green"))
        print(f"Current connections: {usage['connections']} (Target: {MAX_CONNECTIONS})")
        print(colored(f"Host CPU: {usage['cpu']:.1f}% | Memory: {usage['memory']:.1f}%", 
                      "green" if usage['cpu'] < CPU_LIMIT and usage['memory'] < MEMORY_LIMIT else "red"))
        
        if stats["failures"] > 0 and stats["error_types"]:
            print(colored(f"Error types: {stats['error_types']}", "red"))
        
        if TEST_DURATION > 0 and elapsed >= TEST_DURATION:
            print(colored("\nTest duration reached. Preparing to exit...", "yellow"))

async def main():
    # Set TCP connector with more aggressive settings
    connector = aiohttp.TCPConnector(
        limit=0,
        ttl_dns_cache=300,
        use_dns_cache=True,
        ssl=False,
        # In aggressive mode, don't keep connections alive
        force_close=AGGRESSIVE_MODE,  
        enable_cleanup_closed=True,
        keepalive_timeout=15 if not AGGRESSIVE_MODE else 5
    )
    
    # Create a semaphore to limit concurrent connections
    sem = asyncio.Semaphore(MAX_CONNECTIONS)
    
    # Main testing loop
    stats["start_time"] = time.time()
    
    # Start stats and monitoring tasks
    stats_task = asyncio.create_task(print_stats())
    monitor_task = asyncio.create_task(monitor_resources())
    
    print(colored(f"Starting MAXIMUM LOAD TEST on {TARGET_URL} with up to {MAX_CONNECTIONS} concurrent connections", "cyan"))
    print(colored(f"Machine specs: 2 vCPUs, 8GB memory - optimized settings applied", "cyan"))
    print(colored(f"Opening {CONNECTION_RAMP} new connections at a time to avoid system overload", "cyan"))
    
    if TEST_DURATION > 0:
        print(colored(f"Test will run for {TEST_DURATION} seconds", "cyan"))
    else:
        print(colored("Running until manually stopped (Ctrl+C to exit)", "cyan"))
    
    if RAMP_UP_TIME > 0:
        print(colored(f"Ramping up connections over {RAMP_UP_TIME} seconds", "cyan"))
    
    # Use much shorter timeout in ultra mode
    timeout = aiohttp.ClientTimeout(
        total=5 if ULTRA_MODE else REQUEST_TIMEOUT,
        connect=2 if ULTRA_MODE else (5 if AGGRESSIVE_MODE else 15),
        sock_read=5 if ULTRA_MODE else REQUEST_TIMEOUT
    )
    
    # Print mode info
    if ULTRA_MODE:
        print(colored("⚡ ULTRA MODE ENABLED: MAXIMUM PERFORMANCE", "red"))
        print(colored("WARNING: This will generate massive load and may overwhelm the client", "red"))
    
    # Use single session for all requests - FIXED: removed tcp_nodelay parameter
    async with aiohttp.ClientSession(
        connector=connector, 
        timeout=timeout,
        trust_env=True,
        # Skip response parsing in ultra mode
        skip_auto_headers=['Accept-Encoding'] if ULTRA_MODE else None
    ) as session:
        try:
            # Test connection first to see if the site is reachable
            print(f"Testing connection to {TARGET_URL}...")
            try:
                async with session.get(TARGET_URL) as response:
                    print(colored(f"✅ Test connection successful! Status: {response.status}", "green"))
                    print(f"Server: {response.headers.get('Server', 'Unknown')}")
                    print(f"Content-Type: {response.headers.get('Content-Type', 'Unknown')}")
            except Exception as e:
                print(colored(f"⚠️ Test connection failed: {type(e).__name__}: {str(e)}", "red"))
                print("Continuing with load test anyway...")
            
            # Main request loop
            request_id = 0
            start_time = time.time()
            active_connections = 0
            
            # In ultra mode, start with maximum connections immediately
            if ULTRA_MODE:
                active_connections = MAX_CONNECTIONS
                print(colored(f"⚡ ULTRA MODE: Starting with all {active_connections} connections", "red"))
            
            # In aggressive mode, start with a large number of connections immediately
            if AGGRESSIVE_MODE and RAMP_UP_TIME == 0:
                active_connections = MAX_CONNECTIONS // 2  # Start with half to avoid immediate crash
                print(colored(f"AGGRESSIVE MODE: Starting with {active_connections} connections", "red"))
            
            while True:
                # Check resource usage and adjust if needed
                usage = get_system_usage()
                should_throttle = False
                
                if usage["cpu"] > CPU_LIMIT:
                    print(colored(f"CPU usage at {usage['cpu']}% - throttling requests", "yellow"))
                    should_throttle = True
                
                if usage["memory"] > MEMORY_LIMIT:
                    print(colored(f"Memory usage at {usage['memory']}% - throttling requests", "yellow"))
                    should_throttle = True
                    
                # Check if test duration has been reached
                if TEST_DURATION > 0 and time.time() - start_time >= TEST_DURATION:
                    print(colored("Test duration completed!", "yellow"))
                    break
                
                # Calculate ramped connections
                if RAMP_UP_TIME > 0:
                    elapsed = time.time() - start_time
                    if elapsed < RAMP_UP_TIME:
                        # Gradually increase connections from BATCH_SIZE to MAX_CONNECTIONS
                        ramp_factor = elapsed / RAMP_UP_TIME
                        target_connections = int(BATCH_SIZE + (MAX_CONNECTIONS - BATCH_SIZE) * ramp_factor)
                    else:
                        target_connections = MAX_CONNECTIONS
                else:
                    target_connections = MAX_CONNECTIONS
                    
                # Calculate how many new connections to open
                if active_connections < target_connections:
                    # In aggressive mode, open more connections per batch
                    connection_factor = 0.5 if should_throttle else (2.0 if AGGRESSIVE_MODE else 1.0)
                    new_connections = min(int(CONNECTION_RAMP * connection_factor), target_connections - active_connections)
                    
                    if new_connections > 0:
                        active_connections += new_connections
                        print(f"Opening {new_connections} new connections. Total active: {active_connections}/{MAX_CONNECTIONS}")
                        
                        # Create a batch of requests - break into smaller chunks for better parallelism
                        max_chunk = 500 if AGGRESSIVE_MODE else new_connections
                        for i in range(0, new_connections, max_chunk):
                            chunk_size = min(max_chunk, new_connections - i)
                            tasks = []
                            
                            for j in range(chunk_size):
                                task = asyncio.create_task(send_request(session, sem, request_id, active_connections))
                                tasks.append(task)
                                request_id += 1
                            
                            # Fire and almost forget in aggressive mode
                            if AGGRESSIVE_MODE:
                                for task in tasks:
                                    asyncio.create_task(task)  # Don't wait for completion
                                await asyncio.sleep(0.01)  # Tiny sleep to allow event loop to work
                            else:
                                # Wait for all tasks to complete in standard mode
                                results = await asyncio.gather(*tasks, return_exceptions=True)
                                
                                # Process exceptions as before
                                exception_count = 0
                                for result in results:
                                    if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                                        exception_count += 1
                                        if DEBUG_MODE and exception_count < 10:  # Limit excessive error logging
                                            print(f"Task exception: {type(result).__name__}: {str(result)}")
                        
                        # Much shorter pause in aggressive mode
                        await asyncio.sleep(0.01 if AGGRESSIVE_MODE else 0.2)
                else:
                    # Maintenance mode - keep a constant stream of requests
                    # In aggressive mode, much larger maintenance batches
                    maintenance_size = min(BATCH_SIZE * 2 if AGGRESSIVE_MODE else BATCH_SIZE, 
                                         int(active_connections * (0.2 if AGGRESSIVE_MODE else 0.05)))
                    
                    # Process in smaller chunks for better distribution
                    for i in range(0, maintenance_size, 500):
                        chunk_size = min(500, maintenance_size - i)
                        tasks = []
                        
                        for j in range(chunk_size):
                            task = asyncio.create_task(send_request(session, sem, request_id, active_connections))
                            tasks.append(task)
                            request_id += 1
                        
                        # In aggressive mode, don't wait for completion
                        if AGGRESSIVE_MODE:
                            for task in tasks:
                                asyncio.create_task(task)
                            await asyncio.sleep(0.01)  # Minimal pause
                        else:
                            await asyncio.gather(*tasks, return_exceptions=True)
                    
                    # Minimal pause between maintenance batches in aggressive mode
                    await asyncio.sleep(0.01 if AGGRESSIVE_MODE else 0.5)
                
                # Ultra mode: Keep firing massive batches of requests continuously
                if ULTRA_MODE:
                    # Fire batches of 5000 requests at a time without tracking them individually
                    batch_size = 5000
                    for _ in range(20):  # 20 batches = 100K requests in one go
                        tasks = []
                        for j in range(batch_size):
                            task = send_request(session, sem, request_id, MAX_CONNECTIONS)
                            asyncio.create_task(task)
                            request_id += 1
                        
                        # Minimal pause to allow event loop to process
                        await asyncio.sleep(0.001)
                    
                    # Short pause before next mega-batch
                    await asyncio.sleep(0.01)
                    continue
                
        except asyncio.CancelledError:
            print(colored("Load test cancelled", "yellow"))
        except Exception as e:
            print(colored(f"Main loop error: {type(e).__name__}: {str(e)}", "red"))
        finally:
            # Cancel stats and monitoring tasks
            stats_task.cancel()
            monitor_task.cancel()

# Multiprocessing for maximum throughput
def launch_worker(worker_id):
    print(f"Starting worker {worker_id}")
    os.system(f"python {__file__} --no-verify --ultra --connections {MAX_CONNECTIONS // WORKERS}")

if __name__ == "__main__":
    try:
        # Ultra mode with workers - spawn multiple processes for multi-core performance
        if ULTRA_MODE and WORKERS > 1 and not os.environ.get('WORKER_PROCESS'):
            print(colored(f"⚡⚡ LAUNCHING {WORKERS} WORKER PROCESSES FOR MAXIMUM PERFORMANCE ⚡⚡", "red"))
            
            # Mark ourselves as the parent process
            os.environ['WORKER_PROCESS'] = '0'
            
            # Launch worker processes
            processes = []
            for i in range(1, WORKERS):
                cmd = f"python {__file__} --url {TARGET_URL} --ultra --connections {MAX_CONNECTIONS//WORKERS} --timeout {REQUEST_TIMEOUT}"
                os.putenv('WORKER_PROCESS', str(i))
                os.system(f"nohup {cmd} > worker_{i}.log 2>&1 &")
            
            # Continue with the main process as worker 0
            
        # Increase limit of open files on Unix systems
        if os.name == 'posix':
            import resource
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            # Try to set a higher limit for file descriptors to support many connections
            new_limit = min(100000, hard)  # Push to 100K if possible
            resource.setrlimit(resource.RLIMIT_NOFILE, (new_limit, hard))
            print(f"File descriptor limit set to {new_limit}")
            
            # Set process priority to high
            try:
                import os
                os.nice(-10)  # Lower nice value = higher priority
                print("Process priority increased")
            except:
                pass
        
        # Start the event loop with optimal settings
        policy = asyncio.get_event_loop_policy()
        policy.set_event_loop(asyncio.new_event_loop())
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(lambda loop, context: print(f"Async error: {context['message']}") if 'message' in context else None)
        
        # Set larger buffers for better network performance
        try:
            if hasattr(loop, 'sock_sendall_threshold'):
                loop.sock_sendall_threshold = 32768  # Default is 16384
            if hasattr(loop, 'sock_recv_max'):
                loop.sock_recv_max = 65536  # Default is 16384
        except:
            pass
        
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print(colored("\nLoad test stopped by user", "yellow"))
        
        # Calculate final statistics
        if stats["start_time"]:
            elapsed = time.time() - stats["start_time"]
            print(colored(f"\n=== FINAL RESULTS AFTER {elapsed:.1f} SECONDS ===", "cyan"))
            print(colored(f"Total Requests: {stats['requests_sent']}", "cyan"))
            print(f"Requests/second: {stats['requests_sent'] / elapsed if elapsed > 0 else 0:.2f}")
            print(colored(f"Success: {stats['success']} | Failures: {stats['failures']}", "green" if stats["failures"] == 0 else "yellow"))
            print(f"Success rate: {(stats['success']/stats['requests_sent']*100) if stats['requests_sent'] > 0 else 0:.1f}%")
            
            # Calculate response time statistics
            if stats["response_times"]:
                avg_response_time = sum(stats["response_times"]) / len(stats["response_times"])
                print(f"Response time: {avg_response_time*1000:.2f}ms avg, {stats['min_response_time']*1000:.2f}ms min, {stats['max_response_time']*1000:.2f}ms max")
            
            # Calculate bandwidth
            bandwidth_mb = stats["bandwidth_used"] / (1024 * 1024)
            bandwidth_rate = bandwidth_mb / elapsed if elapsed > 0 else 0
            print(f"Bandwidth: {bandwidth_mb:.2f} MB transferred ({bandwidth_rate:.2f} MB/s)")
            
            print(colored(f"Status codes: {stats['status_codes']}", "green"))
            
            if stats["failures"] > 0 and stats["error_types"]:
                print(colored(f"Error types: {stats['error_types']}", "red"))