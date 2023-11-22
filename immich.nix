 # Immich
  virtualisation.oci-containers.containers = {
    immich = {
      autoStart = true;
      image = "ghcr.io/imagegenius/immich:latest";
      volumes = [
        "/mnt/fusion/immich/config:/config"
        "/mnt/fusion/immich/photos:/photos"
        "/mnt/fusion/immich/config/machine-learning:/config/machine-learning"
      ];
      ports = [ "2283:8080" ];
      environment = {
        PUID = "1000";
        PGID = "1000";
        TZ = "Europe/Berlin"; # Change this to your timezone
        DB_HOSTNAME = "postgres14";
        DB_USERNAME = "postgres";
        DB_PASSWORD = "postgres";
        DB_DATABASE_NAME = "immich";
        REDIS_HOSTNAME = "redis";
      };
      extraOptions = [ "--network=immich-net" "--gpus=all" ];
    };

    redis = {
      autoStart = true;
      image = "redis";
      ports = [ "6379:6379" ];
      extraOptions = [ "--network=immich-net" ];
    };

    postgres14 = {
      autoStart = true;
      image = "postgres:14";
      ports = [ "5432:5432" ];
      volumes = [
        "pgdata:/var/lib/postgresql/data"
      ];
      environment = {
        POSTGRES_USER = "postgres";
        POSTGRES_PASSWORD = "postgres";
        POSTGRES_DB = "immich";
      };
      extraOptions = [ "--network=immich-net" ];
    };
  };

}