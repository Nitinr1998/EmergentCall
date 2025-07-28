import requests
import sys
import json
from datetime import datetime

class HospitalAPITester:
    def __init__(self, base_url="https://c73ac634-ba74-46c4-bf79-8d7eab39f14c.preview.emergentagent.com"):
        self.base_url = base_url
        self.api_url = f"{base_url}/api"
        self.tests_run = 0
        self.tests_passed = 0
        self.test_call_sid = None

    def run_test(self, name, method, endpoint, expected_status, data=None, headers=None):
        """Run a single API test"""
        url = f"{self.api_url}/{endpoint}" if endpoint else f"{self.api_url}"
        if headers is None:
            headers = {'Content-Type': 'application/json'}

        self.tests_run += 1
        print(f"\n🔍 Testing {name}...")
        print(f"   URL: {url}")
        
        try:
            if method == 'GET':
                response = requests.get(url, headers=headers, timeout=10)
            elif method == 'POST':
                response = requests.post(url, json=data, headers=headers, timeout=10)

            print(f"   Status Code: {response.status_code}")
            
            success = response.status_code == expected_status
            if success:
                self.tests_passed += 1
                print(f"✅ Passed - Status: {response.status_code}")
                try:
                    response_data = response.json()
                    print(f"   Response: {json.dumps(response_data, indent=2)[:200]}...")
                    return True, response_data
                except:
                    print(f"   Response: {response.text[:200]}...")
                    return True, response.text
            else:
                print(f"❌ Failed - Expected {expected_status}, got {response.status_code}")
                try:
                    error_data = response.json()
                    print(f"   Error: {json.dumps(error_data, indent=2)}")
                except:
                    print(f"   Error: {response.text}")
                return False, {}

        except requests.exceptions.Timeout:
            print(f"❌ Failed - Request timeout")
            return False, {}
        except requests.exceptions.ConnectionError:
            print(f"❌ Failed - Connection error")
            return False, {}
        except Exception as e:
            print(f"❌ Failed - Error: {str(e)}")
            return False, {}

    def test_health_check(self):
        """Test health endpoint"""
        return self.run_test(
            "Health Check",
            "GET",
            "health",
            200
        )

    def test_root_endpoint(self):
        """Test root API endpoint"""
        return self.run_test(
            "Root API Endpoint",
            "GET",
            "",
            200
        )

    def test_get_appointments_empty(self):
        """Test getting appointments (should be empty initially)"""
        return self.run_test(
            "Get Appointments (Empty)",
            "GET",
            "appointments",
            200
        )

    def test_make_call_endpoint(self):
        """Test make-call endpoint (without actually making a call)"""
        test_data = {
            "phone_number": "7567021526",
            "patient_name": "Test Patient"
        }
        
        success, response = self.run_test(
            "Make Call Endpoint",
            "POST",
            "make-call",
            200,
            data=test_data
        )
        
        if success and isinstance(response, dict):
            self.test_call_sid = response.get('call_sid')
            print(f"   Call SID: {self.test_call_sid}")
        
        return success, response

    def test_call_status(self):
        """Test call status endpoint if we have a call SID"""
        if not self.test_call_sid:
            print("\n🔍 Testing Call Status...")
            print("❌ Skipped - No call SID available")
            return False, {}
        
        return self.run_test(
            "Call Status",
            "GET",
            f"call-status/{self.test_call_sid}",
            200
        )

    def test_voice_webhook_without_data(self):
        """Test voice webhook endpoint (should handle missing data gracefully)"""
        return self.run_test(
            "Voice Webhook (No Data)",
            "POST",
            "voice/webhook",
            200,
            data={}
        )

    def test_process_speech_without_data(self):
        """Test process speech endpoint (should handle missing data gracefully)"""
        return self.run_test(
            "Process Speech (No Data)",
            "POST",
            "voice/process-speech",
            200,
            data={}
        )

    def test_invalid_endpoint(self):
        """Test invalid endpoint (should return 404)"""
        return self.run_test(
            "Invalid Endpoint",
            "GET",
            "invalid-endpoint",
            404
        )

    def test_make_call_invalid_data(self):
        """Test make-call with invalid data"""
        test_data = {
            "invalid_field": "test"
        }
        
        success, response = self.run_test(
            "Make Call (Invalid Data)",
            "POST",
            "make-call",
            422,  # Validation error
            data=test_data
        )
        
        return success, response

def main():
    print("🏥 AI Hospital Appointment Booking Agent - API Testing")
    print("=" * 60)
    
    # Setup
    tester = HospitalAPITester()
    
    # Run basic API tests
    print("\n📋 BASIC API TESTS")
    print("-" * 30)
    
    tester.test_health_check()
    tester.test_root_endpoint()
    tester.test_get_appointments_empty()
    
    # Test invalid endpoint
    tester.test_invalid_endpoint()
    
    # Test make-call functionality
    print("\n📞 CALL FUNCTIONALITY TESTS")
    print("-" * 30)
    
    # Test with valid data
    tester.test_make_call_endpoint()
    
    # Test call status if we got a call SID
    tester.test_call_status()
    
    # Test with invalid data
    tester.test_make_call_invalid_data()
    
    # Test voice endpoints
    print("\n🎤 VOICE PROCESSING TESTS")
    print("-" * 30)
    
    tester.test_voice_webhook_without_data()
    tester.test_process_speech_without_data()
    
    # Print final results
    print("\n" + "=" * 60)
    print(f"📊 FINAL RESULTS")
    print(f"Tests Run: {tester.tests_run}")
    print(f"Tests Passed: {tester.tests_passed}")
    print(f"Tests Failed: {tester.tests_run - tester.tests_passed}")
    print(f"Success Rate: {(tester.tests_passed/tester.tests_run)*100:.1f}%")
    
    if tester.tests_passed == tester.tests_run:
        print("🎉 All tests passed!")
        return 0
    else:
        print("⚠️  Some tests failed. Check the details above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())