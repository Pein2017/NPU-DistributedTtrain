import logging
from typing import Any, Dict, Optional, Type

import torch
import torch.distributed as dist
import torch.nn as nn
import torch.optim as optim
from setup_utilis import setup_logger
from torch.optim.lr_scheduler import (
    CosineAnnealingLR,
    OneCycleLR,
    ReduceLROnPlateau,
    StepLR,
    _LRScheduler,
)

optimizer_logger = None
if dist.is_initialized():
    if dist.get_rank() == 0:
        optimizer_logger = setup_logger(
            name="OptimizerProcess",
            log_file_name="optimizer_process.log",
            level=logging.INFO,
            console=False,
        )
        optimizer_logger.debug("Optimizer process logger initialized.")
else:
    raise ValueError(
        "Distributed training is not initialized. Rasied error from optimizer.py"
    )


def initialize_optimizer_manager(model: nn.Module, config: Dict):
    # Extract optimizer parameters from the config
    optimizer_params = {
        "lr": float(config["training"]["lr"]),
        "momentum": float(config["optimizer"].get("momentum", 0.9)),
        "weight_decay": float(config["optimizer"]["weight_decay"]),
        "betas": tuple(float(x) for x in config["optimizer"].get("betas", [0.9, 0.95])),
    }

    # Additional parameters for optimizers that require them
    if "momentum" in config["optimizer"]:
        optimizer_params["momentum"] = float(config["optimizer"]["momentum"])
    if "betas" in config["optimizer"]:
        optimizer_params["betas"] = tuple(
            float(x) for x in config["optimizer"]["betas"]
        )

    # Initialize the OptimizerManager with extracted parameters
    optimizer_manager = OptimizerManager(
        model.parameters(),
        optimizer_type=config["optimizer"]["name"],
        optimizer_params=optimizer_params,
        patience=int(config["early_stopping"]["patience"]),
        min_loss_improvement=float(config["early_stopping"]["min_loss_improvement"]),
    )

    return optimizer_manager


class OptimizerManager:
    """
    Manager class for creating and managing PyTorch optimizers and learning rate schedulers.
    """

    def __init__(
        self,
        model_parameters,
        optimizer_type: str,
        optimizer_params: Dict[str, Any],
        patience: int = 10,
        min_loss_improvement: float = 1e-6,
    ):
        """
        Initialize the OptimizerManager with the specified optimizer type and parameters.
        """
        self.model_parameters = model_parameters
        self.optimizer_params = optimizer_params
        self.optimizer = self.create_optimizer(
            model_parameters, optimizer_type, optimizer_params
        )
        self.scheduler = None  # Placeholder for a learning rate scheduler
        self.patience = patience
        self.min_loss_improvement = min_loss_improvement
        self.best_loss = float("inf")
        self.early_stop_counter = 0

        self.early_stop = False

    def create_optimizer(
        self, parameters, optimizer_type: str, optimizer_params: Dict[str, Any]
    ) -> optim.Optimizer:
        """
        Factory method to create an optimizer based on the type and parameters specified.
        """
        optimizer_cls = getattr(optim, optimizer_type, None)
        if optimizer_cls is None:
            raise ValueError(f"Unsupported optimizer type: {optimizer_type}")

        # Prepare parameters specific to each optimizer to avoid passing unsupported arguments
        if optimizer_type in ["Adam", "AdamW"]:
            # Adam and AdamW do not use 'momentum'
            filtered_params = {
                k: v for k, v in optimizer_params.items() if k != "momentum"
            }
        elif optimizer_type == "SGD":
            # SGD uses 'momentum' but not 'betas'
            filtered_params = {
                k: v for k, v in optimizer_params.items() if k != "betas"
            }
        else:
            # For other optimizers, use all provided parameters
            filtered_params = optimizer_params

        return optimizer_cls(parameters, **filtered_params)

    def update_optimizer_state(self, optimizer_state_dict, params_to_restore=None):
        """
        Update specific states of the optimizer from a state dictionary, while reinitializing others.
        """
        if optimizer_logger is not None:
            optimizer_logger.debug("Updating optimizer state...")

        if params_to_restore is None:
            params_to_restore = []

        # Reinitialize the optimizer to reset all internal states
        self.optimizer = self.create_optimizer(
            self.model_parameters,
            self.optimizer.__class__.__name__,
            self.optimizer_params,
        )

        # Restore the optimizer state
        self.optimizer.load_state_dict(optimizer_state_dict)

        # Optionally restore specific parameters if needed
        if params_to_restore:
            for group in self.optimizer.param_groups:
                for param_name in params_to_restore:
                    original_value = group.get(param_name, "Undefined")
                    restored_value = optimizer_state_dict["param_groups"][0].get(
                        param_name, "Undefined"
                    )
                    group[param_name] = restored_value
                    if optimizer_logger is not None:
                        optimizer_logger.debug(
                            f"Restored {param_name} from {original_value} to {restored_value}"
                        )

    def step(self) -> None:
        """
        Perform a single optimization step.
        """

        self.optimizer.step()

    def zero_grad(self) -> None:
        """
        Clear the gradients of all optimized parameters.
        """
        self.optimizer.zero_grad()

    def check_early_stopping(self, val_loss: float) -> bool:
        """
        Check if early stopping criteria are met based on validation loss.

        Args:
            val_loss (float): The validation loss for the current epoch.
        """
        if (
            self.best_loss is None
            or val_loss < self.best_loss - self.min_loss_improvement
        ):
            self.best_loss = val_loss
            self.early_stop_counter = 0

            if optimizer_logger is not None:
                optimizer_logger.debug("New best loss recorded.")
        else:
            self.early_stop_counter += 1

            if optimizer_logger is not None:
                optimizer_logger.debug(
                    f"No improvement in loss for {self.early_stop_counter} epochs."
                )

        if self.early_stop_counter >= self.patience:
            self.early_stop = True

            if optimizer_logger is not None:
                optimizer_logger.info("Early stopping triggered.")

        return self.early_stop


class WarmUpLR(_LRScheduler):
    def __init__(self, optimizer, total_iters, last_epoch=-1):
        """
        WarmUpLR Scheduler

        Args:
            optimizer (Optimizer): Wrapped optimizer.
            total_iters (int): The total number of iterations for the warmup phase.
            last_epoch (int): The index of the last epoch. Default: -1.
        """
        self.total_iters = total_iters
        super().__init__(optimizer, last_epoch)

    def get_lr(self):
        """
        Calculate the learning rate for the current epoch.

        Returns:
            List of learning rates for each parameter group.
        """
        if self.last_epoch == -1:
            return [0.0 for base_lr in self.base_lrs]
        return [
            base_lr * min(1.0, self.last_epoch / self.total_iters)
            for base_lr in self.base_lrs
        ]


class SchedulerManager:
    """
    Manager class for creating and managing PyTorch learning rate schedulers.
    """

    def __init__(self, optimizer: optim.Optimizer, evaluation_interval: int):
        """
        Initialize the SchedulerManager with the specified optimizer.
        """
        self.optimizer = optimizer
        self.evaluation_interval = evaluation_interval
        self.scheduler = None
        self.warmup_scheduler = None
        self.is_warmup = False

    def create_scheduler(
        self, scheduler_type: str, warmup_steps: int = 0, **kwargs: Any
    ):
        """
        Create a learning rate scheduler based on the type and parameters specified.
        If warmup_steps is provided and greater than 0, a warmup scheduler will also be created.

        Args:
            scheduler_type (str): The type of the scheduler.
            warmup_steps (int): The number of iterations to perform warmup.
            **kwargs (Any): Additional keyword arguments for the scheduler constructor.
        """
        if warmup_steps > 0:
            self.is_warmup = True
            self.warmup_scheduler = WarmUpLR(self.optimizer, total_iters=warmup_steps)

            if optimizer_logger is not None:
                optimizer_logger.info(
                    f"Warmup scheduler created for the first {warmup_steps} iterations."
                )

        scheduler_map = {
            "ReduceLROnPlateau": ReduceLROnPlateau,
            "StepLR": StepLR,
            "CosineAnnealingLR": CosineAnnealingLR,
            "OneCycleLR": OneCycleLR,
        }

        scheduler_cls = scheduler_map.get(scheduler_type)
        if scheduler_cls is None:
            raise ValueError(f"Unsupported scheduler type: {scheduler_type}")

        self.scheduler = scheduler_cls(self.optimizer, **kwargs)
        # Adjust patience for ReduceLROnPlateau based on evaluation interval
        if isinstance(self.scheduler, ReduceLROnPlateau):
            self.scheduler.patience = max(
                1, self.scheduler.patience // self.evaluation_interval
            )

        if optimizer_logger is not None:
            optimizer_logger.debug(f"Scheduler {scheduler_type} created successfully.")

    def scheduler_step(
        self, current_epoch: int, metric: Optional[float] = None, **kwargs
    ):
        """
        Execute a step of the scheduler, typically after an optimizer step.
        If in the warmup phase, the warmup scheduler is updated instead of the main scheduler.

        Args:
            current_epoch (int): The current epoch number.
            metric (optional): Metric to pass to ReduceLROnPlateau scheduler.
        """
        current_lr = self.scheduler.optimizer.param_groups[0]["lr"]

        if self.is_warmup and current_epoch <= self.warmup_scheduler.total_iters:
            self.warmup_scheduler.step(current_epoch)

            if optimizer_logger is not None:
                optimizer_logger.debug(
                    f"Warmup scheduler step executed for epoch {current_epoch}."
                )
        else:
            if isinstance(self.scheduler, ReduceLROnPlateau):
                if metric is None:
                    raise ValueError(
                        "metric value required for ReduceLROnPlateau scheduler."
                    )
                self.scheduler.step(metric, **kwargs)
            else:
                self.scheduler.step(**kwargs)

            updated_lr = self.scheduler.optimizer.param_groups[0]["lr"]

            if current_lr != updated_lr:
                if optimizer_logger is not None:
                    optimizer_logger.info(
                        f"Learning Rate updated from {current_lr} to {updated_lr}"
                    )


class CriterionManager:
    """
    Loss function manager class for creating and managing PyTorch loss functions.
    """

    def __init__(self, criterion_type: str, **kwargs: Any) -> None:
        """
        Initializes the CriterionManager with a specified loss function type.

        Args:
            criterion_type (str): The type of the loss function to create.
            **kwargs (Any): Additional keyword arguments to pass to the loss function constructor.
        """
        self.criterion = self.create_criterion(criterion_type, **kwargs)

    def create_criterion(self, criterion_type: str, **kwargs: Any) -> nn.Module:
        """
        Factory method to instantiate a loss function based on the criterion type.

        Args:
            criterion_type (str): The type of the loss function to instantiate.
            **kwargs (Any): Additional keyword arguments to pass to the loss function constructor.

        Returns:
            nn.Module: An instance of a PyTorch loss function.

        Raises:
            ValueError: If the specified loss function type is not supported.
        """
        criterion_map: Dict[str, Type[nn.Module]] = {
            "CrossEntropyLoss": nn.CrossEntropyLoss,
            "MSELoss": nn.MSELoss,
            # Add other loss functions as needed
        }

        try:
            criterion_class = criterion_map[criterion_type]
            return criterion_class(**kwargs)
        except KeyError:
            supported_types = ", ".join(criterion_map.keys())
            raise ValueError(
                f"Unsupported criterion type: {criterion_type}. Supported types are: {supported_types}"
            )

    def to_device(self, device: torch.device) -> None:
        """
        Moves the loss function to the specified computing device.

        Args:
            device (torch.device): The device to move the loss function to.
        """
        self.criterion: nn.Module = self.criterion.to(device)
