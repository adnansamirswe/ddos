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
parser.add_argument('--url', default="https://speedbuy.xyz", help='Target URL (your own website)')
parser.add_argument('--connections', type=int, default=20000, help='Max concurrent connections')  # Drastically increased
parser.add_argument('--timeout', type=int, default=10, help='Request timeout in seconds')  # Reduced for faster cycling
parser.add_argument('--debug', action='store_true', help='Enable detailed error logging')
parser.add_argument('--duration', type=int, default=0, help='Test duration in seconds (0 = unlimited)')
parser.add_argument('--ramp', type=int, default=10, help='Ramp up time in seconds (reduced)')  # Much faster ramp-up
parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for requests')  # Drastically increased
parser.add_argument('--connection-ramp', type=int, default=2000, help='How many connections to open in each batch')  # Increased
parser.add_argument('--aggressive', action='store_true', help='Enable aggressive mode (faster but may overwhelm client)')
# Add missing arguments
parser.add_argument('--max-response-store', type=int, default=1000, help='Maximum number of response times to store (to limit memory usage)')
parser.add_argument('--cpu-limit', type=float, default=85.0, help='CPU usage percentage limit (will slow down if exceeded)')
parser.add_argument('--memory-limit', type=float, default=80.0, help='Memory usage percentage limit (will slow down if exceeded)')
args = parser.parse_args()

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

# Realistic browser headers
HEADERS_LIST = [
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"},
    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15"},
    {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/119.0"},
    {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"},
]

# Add actual endpoints from your site
ENDPOINTS = ["", "about", "contact", "signup", "login", "pricing"]

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

# Simplified URL generation for speed
def generate_random_url():
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

# Modified send_request for speed in aggressive mode
async def send_request(session, sem, request_id, current_connections):
    async with sem:
        url = generate_random_url()
        
        # In aggressive mode, use minimal headers for speed
        if AGGRESSIVE_MODE:
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
                print(f"Sending {method} request to {url}")
                
            # In aggressive mode, don't use retry mechanism
            if AGGRESSIVE_MODE:
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
                # Original code for non-aggressive mode
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
    
    # Use shorter timeout in aggressive mode
    timeout = aiohttp.ClientTimeout(
        total=REQUEST_TIMEOUT,
        connect=5 if AGGRESSIVE_MODE else 15,
        sock_read=REQUEST_TIMEOUT
    )
    
    # Use single session for all requests - FIXED: removed tcp_nodelay parameter
    async with aiohttp.ClientSession(
        connector=connector, 
        timeout=timeout,
        trust_env=True
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
                
        except asyncio.CancelledError:
            print(colored("Load test cancelled", "yellow"))
        except Exception as e:
            print(colored(f"Main loop error: {type(e).__name__}: {str(e)}", "red"))
        finally:
            # Cancel stats and monitoring tasks
            stats_task.cancel()
            monitor_task.cancel()

if __name__ == "__main__":
    try:
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