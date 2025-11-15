#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Scenario driver that exercises every Train-Ticket microservice to trigger
multi-modal data collection.
"""

import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

# Import shared atomic query helpers
from atomic_queries import (
    BASE_ADDRESS as API_BASE_ADDRESS,
    DEFAULT_HEADERS as DEFAULT_ATOMIC_HEADERS,
    _login,
    _query_high_speed_ticket,
    _query_normal_ticket,
    _query_advanced_ticket,
    _query_orders,
    _query_orders_all_info,
    _pay_one_order,
    _cancel_one_order,
    _collect_one_order,
    _enter_station,
    _rebook_ticket,
    _query_contacts,
    _query_assurances,
    _query_food,
    _put_consign,
    _query_route,
    _query_admin_basic_price,
    _query_admin_basic_config,
    _query_admin_travel,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("test_all_services")

class AllServicesTester:
    def __init__(self, base_address: Optional[str] = None):
        self.base_address = base_address or API_BASE_ADDRESS
        self.headers = self._build_default_headers()
        self.uuid = os.environ.get("TT_USER_UUID", "4d2a46c7-71cb-4cf1-b5bb-b68406d9da6f")
        
    def _build_default_headers(self) -> Dict[str, str]:
        """Seed headers using the shared atomic query defaults."""
        headers = dict(DEFAULT_ATOMIC_HEADERS)
        token = os.environ.get("TT_AUTH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        cookie = os.environ.get("TT_SESSION_COOKIE")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _direct_login(self, username: Optional[str] = None, password: Optional[str] = None) -> Optional[str]:
        """Attempt to obtain a token directly from the login endpoints."""
        username = username or os.environ.get("TT_USERNAME", "fdse_microservice")
        password = password or os.environ.get("TT_PASSWORD", "111111")
        url_candidates = [
            f"{self.base_address}/api/v1/users/login",
            f"{self.base_address}/api/v1/userservice/users/login",
        ]
        for url in url_candidates:
            try:
                resp = requests.post(url, json={"username": username, "password": password},
                                      headers={"Accept": "application/json", "Content-Type": "application/json"},
                                      timeout=10)
            except Exception as e:
                logger.warning(f"direct login request error at {url}: {e}")
                continue

            if resp.status_code != 200:
                logger.warning(f"direct login failed {resp.status_code} at {url}: {resp.text[:200]}")
                continue

            try:
                body = resp.json()
            except Exception:
                logger.warning(f"direct login non-json at {url}: {resp.text[:200]}")
                continue

            data = (body or {}).get("data") or {}
            token = data.get("token")
            if token:
                return token

        return None

    def _make_request(self, method: str, url: str, **kwargs) -> Optional[requests.Response]:
        """Wrapper around requests.request with logging and shared headers."""
        try:
            response = requests.request(method, url, headers=self.headers, **kwargs)
            logger.info(f"{method} {url} - Status: {response.status_code}")
            return response
        except Exception as e:
            logger.error(f"Request failed {method} {url}: {e}")
            return None

    def refresh_token(self):
        """Refresh the bearer token used for downstream calls."""
        token: Optional[str] = None
        try:
            uid, token_from_api = _login()
            if uid and token_from_api:
                token = token_from_api
        except Exception as e:
            logger.warning(f"_login raised exception, will try direct login: {e}")

        if not token:
            token = self._direct_login()

        if token:
            self.headers['Authorization'] = f"Bearer {token}"
            logger.info(f"Token refreshed successfully")
            return True
        logger.error("Failed to refresh token")
        return False

    def test_core_business_services(self):
        """Exercise the core business services end-to-end."""
        logger.info("=== Testing Core Business Services ===")
        
        try:
            # 1. User service - ts-user-service
            logger.info("Testing ts-user-service...")
            self.refresh_token()
            
            # 2. High-speed travel search - ts-travel-service
            logger.info("Testing ts-travel-service...")
            place_pairs = [("Shang Hai", "Su Zhou"), ("Su Zhou", "Shang Hai"), ("Nan Jing", "Shang Hai")]
            for place_pair in place_pairs:
                _query_high_speed_ticket(place_pair=place_pair, headers=self.headers, time=time.strftime("%Y-%m-%d", time.localtime()))
                time.sleep(1)
            
            # 3. Regular travel search - ts-travel2-service  
            logger.info("Testing ts-travel2-service...")
            normal_pairs = [("Shang Hai", "Nan Jing"), ("Nan Jing", "Shang Hai")]
            for place_pair in normal_pairs:
                _query_normal_ticket(place_pair=place_pair, headers=self.headers, time=time.strftime("%Y-%m-%d", time.localtime()))
                time.sleep(1)
                
            # 4. Travel plan service - ts-travel-plan-service
            logger.info("Testing ts-travel-plan-service...")
            plan_types = ["cheapest", "quickest", "minStation"]
            for plan_type in plan_types:
                _query_advanced_ticket(place_pair=("Shang Hai", "Su Zhou"), headers=self.headers, 
                                     time=time.strftime("%Y-%m-%d", time.localtime()), type=plan_type)
                time.sleep(1)
            
            # 5. Order services - ts-order-service & ts-order-other-service
            logger.info("Testing ts-order-service and ts-order-other-service...")
            _query_orders(headers=self.headers, types=tuple([0, 1]), query_other=False)
            _query_orders(headers=self.headers, types=tuple([0, 1]), query_other=True)
            
            # 6. Reservation services - ts-preserve-service & ts-preserve-other-service
            logger.info("Testing preserve services...")
            # Placeholder for full booking flow if needed
            
            # 7. Payment service - ts-inside-payment-service
            logger.info("Testing ts-inside-payment-service...")
            orders = _query_orders(headers=self.headers, types=tuple([0]))
            if orders:
                _pay_one_order(orders[0][0], orders[0][1], headers=self.headers)
            
            # 8. Cancel service - ts-cancel-service
            logger.info("Testing ts-cancel-service...")
            cancel_orders = _query_orders(headers=self.headers, types=tuple([0, 1]))
            if cancel_orders:
                _cancel_one_order(order_id=cancel_orders[0][0], uuid=self.uuid, headers=self.headers)
            
            # 9. Execute service - ts-execute-service (collect ticket, enter station)
            logger.info("Testing ts-execute-service...")
            paid_orders = _query_orders(headers=self.headers, types=tuple([1]))
            if paid_orders:
                _collect_one_order(order_id=paid_orders[0][0], headers=self.headers)
                time.sleep(1)
                _enter_station(order_id=paid_orders[0][0], headers=self.headers)
            
            # 10. Rebook service - ts-rebook-service
            logger.info("Testing ts-rebook-service...")
            rebook_orders = _query_orders(headers=self.headers, types=tuple([1]))
            if rebook_orders:
                _rebook_ticket(old_order_id=rebook_orders[0][0], old_trip_id=rebook_orders[0][1],
                             new_trip_id="D1345", new_date=time.strftime("%Y-%m-%d", time.localtime()),
                             new_seat_type="2", headers=self.headers)
            
        except Exception as e:
            logger.error(f"Error in core business services: {e}")

    def test_auxiliary_services(self):
        """Exercise auxiliary/supporting services."""
        logger.info("=== Testing Auxiliary Services ===")
        
        try:
            # 1. Contact service - ts-contacts-service
            logger.info("Testing ts-contacts-service...")
            _query_contacts(headers=self.headers)
            
            # 2. Assurance service - ts-assurance-service
            logger.info("Testing ts-assurance-service...")
            _query_assurances(headers=self.headers)
            
            # 3. Food service - ts-food-service
            logger.info("Testing ts-food-service...")
            _query_food(headers=self.headers)
            
            # 4. Consign service - ts-consign-service
            logger.info("Testing ts-consign-service...")
            consign_orders = _query_orders_all_info(headers=self.headers)
            if consign_orders:
                _put_consign(result=consign_orders[0], headers=self.headers)
            
            # 5. Route service - ts-route-service
            logger.info("Testing ts-route-service...")
            _query_route(headers=self.headers)
            
            # 6. Verification-code service - ts-verification-code-service
            logger.info("Testing ts-verification-code-service...")
            url = f"{self.base_address}/api/v1/verifycode/generate"
            self._make_request("GET", url)
            
            # 7. Station service - ts-station-service
            logger.info("Testing ts-station-service...")
            stations = ["Shang Hai", "Su Zhou", "Nan Jing"]
            for station in stations:
                url = f"{self.base_address}/api/v1/stationservice/stations/name/{station}"
                self._make_request("GET", url)
                time.sleep(0.5)
            
            # 8. Train service - ts-train-service
            logger.info("Testing ts-train-service...")
            url = f"{self.base_address}/api/v1/trainservice/trains"
            self._make_request("GET", url)
            
            # 9. Price service - ts-price-service
            logger.info("Testing ts-price-service...")
            url = f"{self.base_address}/api/v1/priceservice/prices"
            self._make_request("GET", url)
            
            # 10. Seat service - ts-seat-service
            logger.info("Testing ts-seat-service...")
            url = f"{self.base_address}/api/v1/seatservice/seats"
            self._make_request("GET", url)
            
            # 11. Security service - ts-security-service
            logger.info("Testing ts-security-service...")
            url = f"{self.base_address}/api/v1/securityservice/security/configs"
            self._make_request("GET", url)
            
            # 12. Notification service - ts-notification-service
            logger.info("Testing ts-notification-service...")
            url = f"{self.base_address}/api/v1/notificationservice/notification/preserve_success"
            notification_data = {"email": "test@example.com", "orderId": "test_order"}
            self._make_request("POST", url, json=notification_data)
            
        except Exception as e:
            logger.error(f"Error in auxiliary services: {e}")

    def test_admin_services(self):
        """Exercise administrator-focused services."""
        logger.info("=== Testing Admin Services ===")
        
        try:
            # 1. Admin basic info service - ts-admin-basic-info-service
            logger.info("Testing ts-admin-basic-info-service...")
            _query_admin_basic_price(headers=self.headers)
            _query_admin_basic_config(headers=self.headers)
            
            # 2. Admin travel service - ts-admin-travel-service
            logger.info("Testing ts-admin-travel-service...")
            _query_admin_travel(headers=self.headers)
            
            # 3. Admin order service - ts-admin-order-service
            logger.info("Testing ts-admin-order-service...")
            url = f"{self.base_address}/api/v1/adminorderservice/adminorder"
            self._make_request("GET", url)
            
            # 4. Admin route service - ts-admin-route-service
            logger.info("Testing ts-admin-route-service...")
            url = f"{self.base_address}/api/v1/adminrouteservice/adminroute"
            self._make_request("GET", url)
            
            # 5. Admin user service - ts-admin-user-service
            logger.info("Testing ts-admin-user-service...")
            url = f"{self.base_address}/api/v1/adminuserservice/users"
            self._make_request("GET", url)
            
        except Exception as e:
            logger.error(f"Error in admin services: {e}")

    def test_extended_services(self):
        """Exercise extended/optional services."""
        logger.info("=== Testing Extended Services ===")
        
        try:
            # 1. Auth service - ts-auth-service
            logger.info("Testing ts-auth-service...")
            url = f"{self.base_address}/api/v1/auth/login"
            auth_data = {"username": "test", "password": "test"}
            self._make_request("POST", url, json=auth_data)
            
            # 2. Avatar service - ts-avatar-service
            logger.info("Testing ts-avatar-service...")
            url = f"{self.base_address}/api/v1/avatarservice/avatar/{self.uuid}"
            self._make_request("GET", url)
            
            # 3. Basic service - ts-basic-service
            logger.info("Testing ts-basic-service...")
            endpoints = ["/api/v1/basicservice/basic/travel", "/api/v1/basicservice/basic/stations"]
            for endpoint in endpoints:
                url = f"{self.base_address}{endpoint}"
                self._make_request("GET", url)
                time.sleep(0.5)
            
            # 4. Config service - ts-config-service
            logger.info("Testing ts-config-service...")
            url = f"{self.base_address}/api/v1/configservice/configs"
            self._make_request("GET", url)
            
            # 5. Delivery service - ts-delivery-service
            logger.info("Testing ts-delivery-service...")
            url = f"{self.base_address}/api/v1/deliveryservice/delivery"
            self._make_request("GET", url)
            
            # 6. Food-delivery service - ts-food-delivery-service
            logger.info("Testing ts-food-delivery-service...")
            url = f"{self.base_address}/api/v1/fooddeliveryservice/fooddelivery"
            self._make_request("GET", url)
            
            # 7. News service - ts-news-service
            logger.info("Testing ts-news-service...")
            url = f"{self.base_address}/api/v1/newsservice/news"
            self._make_request("GET", url)
            
            # 8. Payment service - ts-payment-service
            logger.info("Testing ts-payment-service...")
            url = f"{self.base_address}/api/v1/paymentservice/payment"
            self._make_request("GET", url)
            
            # 9. Route-plan service - ts-route-plan-service
            logger.info("Testing ts-route-plan-service...")
            url = f"{self.base_address}/api/v1/routeplanservice/routePlan"
            self._make_request("GET", url)
            
            # 10. Station-food service - ts-station-food-service
            logger.info("Testing ts-station-food-service...")
            url = f"{self.base_address}/api/v1/stationfoodservice/stationfood"
            self._make_request("GET", url)
            
            # 11. Ticket-office service - ts-ticket-office-service
            logger.info("Testing ts-ticket-office-service...")
            url = f"{self.base_address}/api/v1/ticketofficeservice/ticketoffice"
            self._make_request("GET", url)
            
            # 12. Train-food service - ts-train-food-service
            logger.info("Testing ts-train-food-service...")
            url = f"{self.base_address}/api/v1/trainfoodservice/trainfood"
            self._make_request("GET", url)
            
            # 13. Voucher service - ts-voucher-service
            logger.info("Testing ts-voucher-service...")
            url = f"{self.base_address}/api/v1/voucherservice/vouchers"
            self._make_request("GET", url)
            
            # 14. Wait-order service - ts-wait-order-service
            logger.info("Testing ts-wait-order-service...")
            url = f"{self.base_address}/api/v1/waitorderservice/waitorder"
            self._make_request("GET", url)
            
            # 15. Consign-price service - ts-consign-price-service
            logger.info("Testing ts-consign-price-service...")
            url = f"{self.base_address}/api/v1/consignpriceservice/consignprice"
            self._make_request("GET", url)
            
        except Exception as e:
            logger.error(f"Error in extended services: {e}")

    def run_complete_business_flow(self):
        """Run a condensed multi-step booking flow."""
        logger.info("=== Running Complete Business Flow ===")
        
        try:
            # 1. Query tickets
            logger.info("Step 1: Querying tickets...")
            trip_ids = _query_high_speed_ticket(place_pair=("Shang Hai", "Su Zhou"), headers=self.headers)
            
            if not trip_ids:
                logger.warning("No tickets found, skipping business flow")
                return
            
            # 2. Fetch auxiliary information
            logger.info("Step 2: Querying auxiliary info...")
            _query_contacts(headers=self.headers)
            _query_assurances(headers=self.headers) 
            _query_food(headers=self.headers)
            
            # 3. Reserve tickets (simplified placeholder)
            logger.info("Step 3: Making reservation...")
            # Real reservations require a full payload; this only exercises the service
            
            # 4. Query orders
            logger.info("Step 4: Querying orders...")
            orders = _query_orders(headers=self.headers, types=tuple([0, 1]))
            
            if orders:
                # 5. Pay the order
                logger.info("Step 5: Paying order...")
                _pay_one_order(orders[0][0], orders[0][1], headers=self.headers)
                
                # 6. Collect the ticket
                logger.info("Step 6: Collecting ticket...")
                _collect_one_order(orders[0][0], headers=self.headers)
                
                # 7. Enter the station
                logger.info("Step 7: Entering station...")
                _enter_station(orders[0][0], headers=self.headers)
            
        except Exception as e:
            logger.error(f"Error in complete business flow: {e}")

    def run_all_services_test(self, iterations=1):
        """Run all service categories sequentially for the given number of iterations."""
        logger.info(f"Starting comprehensive test of ALL services ({iterations} iterations)")
        logger.info("=" * 80)
        
        start_time = time.time()
        
        for i in range(iterations):
            logger.info(f"\n--- Iteration {i+1}/{iterations} ---")
            
            # Periodically refresh the token
            if i % 10 == 0:
                self.refresh_token()
            
            # Exercise each service group
            self.test_core_business_services()
            time.sleep(2)
            
            self.test_auxiliary_services()
            time.sleep(2)
            
            self.test_admin_services()
            time.sleep(2)
            
            self.test_extended_services()
            time.sleep(2)
            
            # Run the condensed business flow
            self.run_complete_business_flow()
            
            logger.info(f"Completed iteration {i+1}/{iterations}")
            time.sleep(5)  # Pause between iterations
        
        total_time = time.time() - start_time
        logger.info(f"All services test completed in {total_time:.2f} seconds")
        logger.info("=" * 80)

def main():
    """Entrypoint used when invoking the script directly."""
    tester = AllServicesTester()
    
    # Run the comprehensive service test (override TT_TEST_ITERATIONS if needed)
    iterations = int(os.environ.get("TT_TEST_ITERATIONS", "1"))
    tester.run_all_services_test(iterations=iterations)

if __name__ == '__main__':
    main()
