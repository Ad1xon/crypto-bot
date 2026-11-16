cmake_minimum_required(VERSION 3.18)

project(crypto_core VERSION 0.1.0 LANGUAGES CXX)

if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Pass -DPython_EXECUTABLE=... to build against a specific venv.
set(PYBIND11_FINDPYTHON ON)
find_package(Python 3.11 REQUIRED COMPONENTS Interpreter Development.Module)

include(FetchContent)
FetchContent_Declare(
    pybind11
    URL https://github.com/pybind/pybind11/archive/refs/tags/v3.1.0.tar.gz
    URL_HASH SHA256=ef712655692a2e9bf7bb7874c022564a45f91d847ddee987e720cd9e28849665
    DOWNLOAD_EXTRACT_TIMESTAMP TRUE
)
FetchContent_MakeAvailable(pybind11)

pybind11_add_module(crypto_core bindings.cpp orderbook.cpp)

target_compile_definitions(crypto_core PRIVATE CRYPTO_CORE_VERSION="${PROJECT_VERSION}")

# No -march=native: the Docker image has to run on whatever CPU the VPS has.
if(MSVC)
    target_compile_options(crypto_core PRIVATE /O2 /W4)
else()
    target_compile_options(crypto_core PRIVATE -O3 -Wall -Wextra -Wpedantic)
endif()

# Install with `cmake --install build --prefix <site-packages>` so `import crypto_core` works.
install(TARGETS crypto_core LIBRARY DESTINATION .)
install(FILES crypto_core.pyi DESTINATION .)
