#define _WIN32_WINNT 0x0600
#include <stdio.h>
#include <stdlib.h>
#include <windows.h>
#include <process.h>
#include <time.h>
#include <string.h>
#include <winsock2.h>
#include <stdarg.h>
#include <limits.h>
#include <ctype.h>
#pragma comment(lib, "Ws2_32.lib")

/*  ----------  CONSTANTS  ----------  */
#define NUM_RUNWAYS   3
#define NUM_PLANES    10
#define SERVER_PORT   54321
#define MAX_CONCURRENT_THREADS 100

/*  ----------  GLOBALS  ----------  */
HANDLE hRunwaySemaphore;
CRITICAL_SECTION cs_print;
CRITICAL_SECTION cs_socket;  // NEW: protect socket writes
SOCKET client_socket = INVALID_SOCKET;
SOCKET listen_socket  = INVALID_SOCKET;

int runway_status[NUM_RUNWAYS];
CRITICAL_SECTION cs_runway_status;

typedef struct {
    int plane_id;
    int priority;          /* 1 = highest */
    HANDLE hSignalEvent;
} WaitingPlane;

WaitingPlane g_priority_queue[MAX_CONCURRENT_THREADS];
int g_queue_size = 0;
CRITICAL_SECTION cs_scheduler;

typedef struct {
    int plane_id;
    int priority;
} plane_arg_t;

int g_user_priorities[NUM_PLANES];

/*  ----------  PROTOTYPES  ----------  */
void tprintf(const char *fmt, ...);
unsigned __stdcall plane_thread_func(void *arg);
void send_state_to_client(const char *msg);
int  acquire_visual_runway(void);
void release_visual_runway(int idx);
void insert_into_priority_queue(WaitingPlane p);
WaitingPlane get_highest_priority_plane(void);

/*  ----------  THREAD-SAFE PRINT  ----------  */
void tprintf(const char *fmt, ...) {
    EnterCriticalSection(&cs_print);
    va_list ap;
    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    LeaveCriticalSection(&cs_print);
}

/*  ----------  VISUAL RUNWAY  ----------  */
int acquire_visual_runway(void) {
    EnterCriticalSection(&cs_runway_status);
    for (int i = 0; i < NUM_RUNWAYS; ++i)
        if (runway_status[i] == 0) { 
            runway_status[i] = 1; 
            LeaveCriticalSection(&cs_runway_status); 
            return i; 
        }
    LeaveCriticalSection(&cs_runway_status);
    return -1;
}

void release_visual_runway(int idx) {
    EnterCriticalSection(&cs_runway_status);
    if (idx >= 0 && idx < NUM_RUNWAYS) runway_status[idx] = 0;
    LeaveCriticalSection(&cs_runway_status);
}

/*  ----------  PRIORITY QUEUE (FIXED: 1 = highest, 10 = lowest)  ----------  */
void insert_into_priority_queue(WaitingPlane p) {
    EnterCriticalSection(&cs_scheduler);
    int i = g_queue_size++;
    // Insert so that lowest priority number (highest priority) is at front
    // Shift elements with HIGHER priority numbers (lower priority) to the right
    while (i > 0 && g_priority_queue[i - 1].priority > p.priority) {
        g_priority_queue[i] = g_priority_queue[i - 1];
        --i;
    }
    g_priority_queue[i] = p;
    tprintf("[QUEUE] Inserted Plane %d (Priority %d) at position %d. Queue size: %d\n", 
            p.plane_id, p.priority, i, g_queue_size);
    LeaveCriticalSection(&cs_scheduler);
}

WaitingPlane get_highest_priority_plane(void) {
    EnterCriticalSection(&cs_scheduler);
    WaitingPlane p = {0, INT_MAX, NULL};
    if (g_queue_size > 0) {
        p = g_priority_queue[0];  // Front of queue has lowest priority number (highest priority)
        tprintf("[QUEUE] Removing Plane %d (Priority %d) from front. New queue size: %d\n",
                p.plane_id, p.priority, g_queue_size - 1);
        for (int i = 0; i < g_queue_size - 1; ++i) 
            g_priority_queue[i] = g_priority_queue[i + 1];
        --g_queue_size;
    }
    LeaveCriticalSection(&cs_scheduler);
    return p;
}

/*  ----------  SOCKET HELPER (FIXED & THREAD-SAFE)  ----------  */
void send_state_to_client(const char *msg) {
    EnterCriticalSection(&cs_socket);
    if (client_socket == INVALID_SOCKET) {
        LeaveCriticalSection(&cs_socket);
        return;
    }
    char buf[256];
    snprintf(buf, sizeof(buf), "%s\n", msg);
    int result = send(client_socket, buf, (int)strlen(buf), 0);
    if (result == SOCKET_ERROR) {
        tprintf("[WARNING] Failed to send message to GUI: %s\n", msg);
    } else {
        tprintf("[IPC->GUI] Sent: %s", buf);  // buf already has \n
    }
    LeaveCriticalSection(&cs_socket);
}

/*  ----------  MAIN  ----------  */
int main(void) {
    WSADATA wsa;
    struct sockaddr_in server_addr = {0};
    struct sockaddr_in client_addr = {0};
    int client_len = sizeof(client_addr);

    srand((unsigned)time(NULL));

    InitializeCriticalSection(&cs_print);
    InitializeCriticalSection(&cs_socket);
    InitializeCriticalSection(&cs_runway_status);
    InitializeCriticalSection(&cs_scheduler);

    hRunwaySemaphore = CreateSemaphore(NULL, NUM_RUNWAYS, NUM_RUNWAYS, NULL);
    for (int i = 0; i < NUM_RUNWAYS; ++i) runway_status[i] = 0;

    tprintf("\n--- Windows C Backend: Runway Manager (Waiting for User Config) ---\n");

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) { 
        tprintf("WSAStartup failed\n"); 
        return 1; 
    }
    
    listen_socket = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listen_socket == INVALID_SOCKET) {
        tprintf("Socket creation failed\n");
        WSACleanup();
        return 1;
    }

    server_addr.sin_family = AF_INET;
    server_addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    server_addr.sin_port = htons(SERVER_PORT);
    
    if (bind(listen_socket, (struct sockaddr *)&server_addr, sizeof(server_addr)) == SOCKET_ERROR) {
        tprintf("Bind failed\n");
        closesocket(listen_socket);
        WSACleanup();
        return 1;
    }
    
    if (listen(listen_socket, 1) == SOCKET_ERROR) {
        tprintf("Listen failed\n");
        closesocket(listen_socket);
        WSACleanup();
        return 1;
    }
    
    tprintf("Listening for Python GUI on 127.0.0.1:%d\n", SERVER_PORT);

    client_socket = accept(listen_socket, (struct sockaddr *)&client_addr, &client_len);
    if (client_socket == INVALID_SOCKET) {
        tprintf("Accept failed\n");
        closesocket(listen_socket);
        WSACleanup();
        return 1;
    }
    
    tprintf("[IPC] GUI connected. Waiting for CONFIG command with priorities.\n");

    /* ----  read CONFIG with proper timeout  ---- */
    char cmd[1024];
    int n = 0;
    int total_received = 0;
    int max_attempts = 100;  // 10 seconds total
    int attempt = 0;

    tprintf("[IPC] Waiting for CONFIG command...\n");

    // Set socket to non-blocking mode temporarily
    u_long mode = 1;
    ioctlsocket(client_socket, FIONBIO, &mode);

    while (attempt < max_attempts && total_received < sizeof(cmd) - 1) {
        n = recv(client_socket, cmd + total_received, sizeof(cmd) - 1 - total_received, 0);
        
        if (n > 0) {
            total_received += n;
            cmd[total_received] = 0;
            
            // Check if we have a complete command (ends with \n or \r\n)
            if (strchr(cmd, '\n') != NULL) {
                tprintf("[IPC] Received complete CONFIG command (%d bytes)\n", total_received);
                break;
            }
        } else if (n == SOCKET_ERROR) {
            int err = WSAGetLastError();
            if (err != WSAEWOULDBLOCK) {
                tprintf("[ERROR] Socket error: %d\n", err);
                closesocket(client_socket);
                closesocket(listen_socket);
                WSACleanup();
                return 1;
            }
        }
        
        Sleep(100);  // Wait 100ms before trying again
        attempt++;
    }

    // Set socket back to blocking mode
    mode = 0;
    ioctlsocket(client_socket, FIONBIO, &mode);

    if (total_received == 0) {
        tprintf("[ERROR] No CONFIG received within timeout\n");
        closesocket(client_socket);
        closesocket(listen_socket);
        WSACleanup();
        return 1;
    }

    cmd[total_received] = 0;
    tprintf("[DEBUG] Received: %s\n", cmd);

    char *tok = strtok(cmd, ",\r\n");
    if (!tok || strcmp(tok, "CONFIG")) {
        tprintf("[ERROR] Expected CONFIG, got: %s\n", tok ? tok : "NULL");
        closesocket(client_socket);
        closesocket(listen_socket);
        WSACleanup();
        return 1;
    }

    strtok(NULL, ",\r\n"); /* skip runways */
    strtok(NULL, ",\r\n"); /* skip planes  */

    int i = 0;
    while ((tok = strtok(NULL, ",\r\n")) && i < NUM_PLANES) {
        g_user_priorities[i++] = atoi(tok);
    }

    if (i != NUM_PLANES) {
        tprintf("[ERROR] Expected %d priorities, got %d\n", NUM_PLANES, i);
        closesocket(client_socket);
        closesocket(listen_socket);
        WSACleanup();
        return 1;
    }

    tprintf("[CONFIG] Received and stored %d user-defined priorities.\n", NUM_PLANES);
    tprintf("[CONFIG] Priorities: ");
    for (int j = 0; j < NUM_PLANES; j++) {
        tprintf("%d ", g_user_priorities[j]);
    }
    tprintf("\n");

    // Give Python GUI a moment to be ready to receive messages
    Sleep(200);

    /* ----  spawn plane threads with staggered start to ensure fair priority ordering  ---- */
    HANDLE threads[NUM_PLANES];
    
    // First, create all threads in suspended state to ensure they all exist
    tprintf("[SIM] Creating all plane threads...\n");
    for (int j = 0; j < NUM_PLANES; ++j) {
        plane_arg_t *arg = (plane_arg_t *)malloc(sizeof(plane_arg_t));
        arg->plane_id = j + 1;
        arg->priority = g_user_priorities[j];
        threads[j] = (HANDLE)_beginthreadex(NULL, 0, plane_thread_func, arg, CREATE_SUSPENDED, NULL);
    }
    
    // Now resume threads in priority order (1=highest priority goes first)
    tprintf("[SIM] Starting threads in priority order...\n");
    
    // Create array of (priority, thread_index) pairs
    typedef struct { int priority; int index; } PriorityIndex;
    PriorityIndex order[NUM_PLANES];
    for (int j = 0; j < NUM_PLANES; ++j) {
        order[j].priority = g_user_priorities[j];
        order[j].index = j;
    }
    
    // Sort by priority (ascending: 1 comes before 10)
    for (int i = 0; i < NUM_PLANES - 1; ++i) {
        for (int j = i + 1; j < NUM_PLANES; ++j) {
            if (order[j].priority < order[i].priority) {
                PriorityIndex temp = order[i];
                order[i] = order[j];
                order[j] = temp;
            }
        }
    }
    
    // Resume threads in priority order
    for (int j = 0; j < NUM_PLANES; ++j) {
        int idx = order[j].index;
        tprintf("[SIM] Starting Plane %d (Priority %d)\n", idx + 1, g_user_priorities[idx]);
        ResumeThread(threads[idx]);
        Sleep(100);  // Small delay to ensure ordering
    }
    
    tprintf("[SIM] All threads started. Simulation is running.\n");

    /* ----  wait for all planes to finish  ---- */
    WaitForMultipleObjects(NUM_PLANES, threads, TRUE, INFINITE);

    tprintf("\n--- All planes processed successfully ---\n");

    /* ----  clean-up  ---- */
    for (int i = 0; i < NUM_PLANES; ++i) CloseHandle(threads[i]);
    CloseHandle(hRunwaySemaphore);
    closesocket(client_socket);
    closesocket(listen_socket);
    WSACleanup();
    DeleteCriticalSection(&cs_print);
    DeleteCriticalSection(&cs_socket);
    DeleteCriticalSection(&cs_runway_status);
    DeleteCriticalSection(&cs_scheduler);
    
    tprintf("Press <Enter> to exit...");
    getchar();
    
    return 0;
}

/*  ----------  PLANE THREAD  ----------  */
unsigned __stdcall plane_thread_func(void *arg) {
    plane_arg_t *p = (plane_arg_t *)arg;
    int plane_id = p->plane_id;
    int priority = p->priority;
    free(p);

    double runway_time = (rand() % 3) + 2.0;
    double progress = 0.0;
    double slice   = 0.05;
    char buf[128];

    tprintf("[PLANE %d] Started with priority %d, needs %.2f seconds\n", 
            plane_id, priority, runway_time);

    // Send WAITING state
    snprintf(buf, sizeof(buf), "%d,WAITING,0,0.0", plane_id);
    send_state_to_client(buf);

    DWORD w = WaitForSingleObject(hRunwaySemaphore, 0);
    HANDLE hEvt = NULL;
    if (w != WAIT_OBJECT_0) {
        tprintf("[PLANE %d] No runway available, entering priority queue\n", plane_id);
        hEvt = CreateEvent(NULL, FALSE, FALSE, NULL);
        WaitingPlane wp = {plane_id, priority, hEvt};
        insert_into_priority_queue(wp);
        WaitForSingleObject(hEvt, INFINITE);
        CloseHandle(hEvt);
        tprintf("[PLANE %d] Signaled from priority queue\n", plane_id);
    }

    int rw = acquire_visual_runway();
    while (rw == -1) {
        Sleep(50);
        rw = acquire_visual_runway();
    }

    tprintf("[PLANE %d] Acquired runway %d\n", plane_id, rw + 1);

    // Send RUNNING state
    snprintf(buf, sizeof(buf), "%d,RUNNING,%d,%.2f", plane_id, rw + 1, runway_time);
    send_state_to_client(buf);

    double elapsed = 0.0;
    while (elapsed < runway_time) {
        Sleep((DWORD)(slice * 1000));
        elapsed += slice;
        double frac = elapsed / runway_time;
        if (frac > 1.0) frac = 1.0;
        
        // Send PROGRESS state
        snprintf(buf, sizeof(buf), "%d,PROGRESS,%d,%.2f", plane_id, rw + 1, frac);
        send_state_to_client(buf);
    }

    // Send COMPLETED state
    snprintf(buf, sizeof(buf), "%d,COMPLETED,%d,1.0", plane_id, rw + 1);
    send_state_to_client(buf);

    tprintf("[PLANE %d] Completed on runway %d\n", plane_id, rw + 1);

    release_visual_runway(rw);

    EnterCriticalSection(&cs_scheduler);
    if (g_queue_size > 0) {
        WaitingPlane next = get_highest_priority_plane();
        tprintf("[PLANE %d] Signaling plane %d (priority %d) from queue\n", 
                plane_id, next.plane_id, next.priority);
        ReleaseSemaphore(hRunwaySemaphore, 1, NULL);
        SetEvent(next.hSignalEvent);
    } else {
        ReleaseSemaphore(hRunwaySemaphore, 1, NULL);
    }
    LeaveCriticalSection(&cs_scheduler);

    _endthreadex(0);
    return 0;
}